"""Stores and refreshes the stdio user's Zendesk OAuth tokens.

The tokens live in one JSON file, `~/.config/zendesk-mcp/tokens.json` (0600; the directory is
0700). Several Claude Code sessions can share it: a refresh runs under a per-store thread lock and
an `fcntl.flock` on the separate `tokens.lock`, re-reads the file under the lock, and skips the
refresh when another process has already rotated the tokens. Zendesk refresh tokens are single
use, so refreshing twice with the same one would sign a session out.

Tokens never appear in a repr, an error message, or a chained exception, and the module does no
logging. It reads no env vars, and the caller passes in the `httpx.Client`. Login lives in
`stdio_login.py`.

Never import `server` from here: `server.py` checks its env vars at import time and will import
this module, so an import back would make a cycle.
"""

import contextlib
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

try:
    import fcntl
except ImportError:  # Windows: TokenStore refuses to start, so the caller can fall back.
    fcntl = None  # type: ignore[assignment]

DEFAULT_TOKEN_DIR = Path.home() / ".config" / "zendesk-mcp"
TOKEN_FILE_NAME = "tokens.json"
LOCK_FILE_NAME = "tokens.lock"
TOKEN_FILE_VERSION = 1

# Requested on every token request. Zendesk allows 300-172800 s for the access token, and
# 604800-7776000 s (90 days) for the refresh token; the access lifetime must be the shorter one.
REFRESH_TOKEN_LIFETIME_SECONDS = 7_776_000
ACCESS_TOKEN_LIFETIME_SECONDS = 3_600
# Refresh when this little time is left, to cover clock drift and a slow request.
REFRESH_SKEW_SECONDS = 120
# httpx applies this per phase, not to the whole request: the read timeout restarts on every read,
# so a slow server can hold one request, and the lock, longer than 10 s with no upper limit. The
# lock timeout is well above the normal case. A waiter that times out has sent nothing, so it
# never causes a second refresh; its next call re-reads the file.
TOKEN_REQUEST_TIMEOUT_SECONDS = 10.0
LOCK_TIMEOUT_SECONDS = 60.0
_FLOCK_POLL_SECONDS = 0.05

# An OAuth `error` value goes into exception text only when it fully matches this.
_OAUTH_ERROR_CODE = re.compile(r"[a-z_]{1,64}")

LOGIN_HINT = "To sign in, run .venv/bin/python stdio_login.py in the folder that holds server.py."
_SIGN_IN_EXPIRED_MESSAGE = f"Zendesk sign-in expired or was revoked. {LOGIN_HINT}"
_NO_SIGN_IN_MESSAGE = f"No Zendesk sign-in found. {LOGIN_HINT}"
_OTHER_ACCOUNT_MESSAGE = (
    f"The saved Zendesk sign-in is for another subdomain or client. {LOGIN_HINT}"
)
_UNREADABLE_FILE_MESSAGE = f"The Zendesk token file is unreadable. {LOGIN_HINT}"
_MALFORMED_RESPONSE_MESSAGE = "Zendesk token request failed (malformed response, unknown)"
_LOCK_TIMEOUT_MESSAGE = "Timed out waiting for the token lock."

_STRING_FIELDS = ("subdomain", "client_id", "access_token", "refresh_token", "scope")
_INTEGER_FIELDS = ("access_token_expires_at", "obtained_at")


class TokenStoreError(Exception):
    """The token store failed: a corrupt file, open permissions, a lock timeout, or no `fcntl`."""


class LoginRequiredError(TokenStoreError):
    """The user must sign in again: no tokens, tokens for another instance, or a dead refresh token."""


class TokenRefreshError(TokenStoreError):
    """A token request failed in a way worth retrying later. The token file is unchanged."""


def _is_int(value: object) -> bool:
    """Return True for an int that is not a bool (JSON `true` loads as a bool)."""
    return type(value) is int


@dataclass(frozen=True)
class TokenSet:
    """One set of OAuth tokens, as stored on disk. Its repr never shows the tokens.

    Attributes:
        subdomain: The Zendesk subdomain the tokens belong to.
        client_id: The OAuth client's Identifier string (not its numeric ID).
        access_token: The bearer token for API requests.
        refresh_token: The single-use token that gets the next set.
        scope: Space-separated scopes (for example "read write"). A refresh keeps only scopes the
            previous set had, so it never widens.
        access_token_expires_at: Epoch seconds (wall clock) when the access token expires.
        refresh_token_expires_at: Epoch seconds when the refresh token expires, or None when
            Zendesk did not say.
        obtained_at: Epoch seconds when the set was issued.
    """

    subdomain: str
    client_id: str
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    scope: str
    access_token_expires_at: int
    refresh_token_expires_at: int | None
    obtained_at: int

    @property
    def scopes(self) -> frozenset[str]:
        """The saved scopes as a set."""
        return frozenset(self.scope.split())

    def expires_soon(self, now: float) -> bool:
        """Return True when `REFRESH_SKEW_SECONDS` or less are left on the access token.

        Args:
            now: The current wall-clock time in epoch seconds.
        """
        return self.access_token_expires_at - REFRESH_SKEW_SECONDS <= now

    def refresh_token_expired(self, now: float) -> bool:
        """Return True when the refresh token is known to have expired.

        Args:
            now: The current wall-clock time in epoch seconds.
        """
        return self.refresh_token_expires_at is not None and self.refresh_token_expires_at <= now

    def to_json(self) -> dict[str, Any]:
        """Return the on-disk JSON object (schema version 1)."""
        return {
            "version": TOKEN_FILE_VERSION,
            "subdomain": self.subdomain,
            "client_id": self.client_id,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
            "access_token_expires_at": self.access_token_expires_at,
            "refresh_token_expires_at": self.refresh_token_expires_at,
            "obtained_at": self.obtained_at,
        }

    @classmethod
    def from_json(cls, data: object) -> "TokenSet":
        """Build a token set from the on-disk JSON object.

        Args:
            data: The parsed contents of the token file.

        Returns:
            The token set.

        Raises:
            TokenStoreError: The data is not an object, or a field is missing or has the wrong
                type. A missing `refresh_token_expires_at` is not an error: it loads as None
                (unknown), like `null`. The message never includes the file's contents.
        """
        if not isinstance(data, dict):
            raise TokenStoreError(_UNREADABLE_FILE_MESSAGE)
        for name in _STRING_FIELDS:
            if not isinstance(data.get(name), str):
                raise TokenStoreError(_UNREADABLE_FILE_MESSAGE)
        for name in _INTEGER_FIELDS:
            if not _is_int(data.get(name)):
                raise TokenStoreError(_UNREADABLE_FILE_MESSAGE)
        refresh_expires_at = data.get("refresh_token_expires_at")
        if refresh_expires_at is not None and not _is_int(refresh_expires_at):
            raise TokenStoreError(_UNREADABLE_FILE_MESSAGE)
        return cls(
            subdomain=data["subdomain"],
            client_id=data["client_id"],
            access_token=data["access_token"],
            refresh_token=data["refresh_token"],
            scope=data["scope"],
            access_token_expires_at=data["access_token_expires_at"],
            refresh_token_expires_at=refresh_expires_at,
            obtained_at=data["obtained_at"],
        )

    @classmethod
    def from_token_response(
        cls,
        body: object,
        *,
        subdomain: str,
        client_id: str,
        now: float,
        previous: "TokenSet | None",
        requested_scope: str,
    ) -> "TokenSet":
        """Build a token set from a successful `/oauth/tokens` answer.

        Expiry times come from the answer's `expires_in`/`refresh_token_expires_in`. Zendesk has
        already rotated when a 2xx arrives, so an answer with both tokens is always kept: an
        invalid `expires_in` falls back to `ACCESS_TOKEN_LIFETIME_SECONDS`, and an invalid
        `refresh_token_expires_in` gives `None` (unknown).

        Args:
            body: The parsed JSON answer.
            subdomain: The Zendesk subdomain the tokens belong to.
            client_id: The OAuth client's Identifier string.
            now: The current wall-clock time in epoch seconds.
            previous: The set being refreshed, or None for a first sign-in. Its scope is kept
                when the answer has no `scope`. A string `scope` in the answer is clipped to the
                previous scopes.
            requested_scope: The `scope` sent with the request ("" if none), used when the answer
                has no `scope` and there is no `previous`.

        Returns:
            The new token set.

        Raises:
            TokenRefreshError: The answer is not a JSON object, or `access_token` or
                `refresh_token` is missing or not a string.
        """
        if not isinstance(body, dict):
            raise TokenRefreshError(_MALFORMED_RESPONSE_MESSAGE)
        if not isinstance(body.get("access_token"), str):
            raise TokenRefreshError(_MALFORMED_RESPONSE_MESSAGE)
        if not isinstance(body.get("refresh_token"), str):
            raise TokenRefreshError(_MALFORMED_RESPONSE_MESSAGE)

        scope = body.get("scope")
        if not isinstance(scope, str):
            scope = previous.scope if previous is not None else requested_scope
        elif previous is not None:
            # A refresh never widens the saved scope: keep only scopes the previous set had.
            scope = " ".join(sorted(previous.scopes & frozenset(scope.split())))
        issued_at = int(now)
        access_lifetime = body.get("expires_in")
        if not _is_int(access_lifetime):
            access_lifetime = ACCESS_TOKEN_LIFETIME_SECONDS
        refresh_lifetime = body.get("refresh_token_expires_in")
        refresh_expires_at = None
        if _is_int(refresh_lifetime):
            refresh_expires_at = issued_at + refresh_lifetime
        return cls(
            subdomain=subdomain,
            client_id=client_id,
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            scope=scope,
            access_token_expires_at=issued_at + access_lifetime,
            refresh_token_expires_at=refresh_expires_at,
            obtained_at=issued_at,
        )


def _safe_error_code(body: object) -> str:
    """Return the answer's OAuth `error` code if it is a plain code, else "unknown"."""
    value = body.get("error") if isinstance(body, dict) else None
    if isinstance(value, str) and _OAUTH_ERROR_CODE.fullmatch(value):
        return value
    return "unknown"


def _response_json(response: httpx.Response) -> object:
    """Return the parsed JSON body, or None when the body is not JSON."""
    try:
        return response.json()
    except ValueError:
        return None


def _raise_for_token_error(status_code: int, body: object) -> None:
    """Raise the store's error for a failed token answer; return for a 2xx.

    Args:
        status_code: The HTTP status of the answer.
        body: The parsed JSON body, or None.

    Raises:
        LoginRequiredError: A non-2xx answer whose `error` is exactly `invalid_grant`, at any
            error status (Zendesk does not document which one it uses).
        TokenRefreshError: Any other non-2xx answer.
    """
    if 200 <= status_code < 300:
        return
    error_code = _safe_error_code(body)
    if error_code == "invalid_grant":
        raise LoginRequiredError(_SIGN_IN_EXPIRED_MESSAGE)
    raise TokenRefreshError(f"Zendesk token request failed ({status_code}, {error_code})")


class TokenStore:
    """The stdio user's token file, with locked, rotation-aware refresh.

    Args:
        subdomain: The Zendesk subdomain; a token file for another one is refused.
        client_id: The OAuth client's Identifier string; a token file for another one is refused.
        http_client: The client for token requests. The store never builds its own.
        directory: Where `tokens.json` and `tokens.lock` live.
        token_url: The token endpoint. Defaults to `https://{subdomain}.zendesk.com/oauth/tokens`;
            tests point it at a stub.
        clock: Wall-clock time in epoch seconds, for token expiry only.
        lock_timeout_seconds: How long to wait for the token lock, in total.

    Raises:
        TokenStoreError: `fcntl` is not available (Windows).
    """

    def __init__(
        self,
        *,
        subdomain: str,
        client_id: str,
        http_client: httpx.Client,
        directory: Path = DEFAULT_TOKEN_DIR,
        token_url: str | None = None,
        clock: Callable[[], float] = time.time,
        lock_timeout_seconds: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        if fcntl is None:
            raise TokenStoreError("The OAuth token store needs macOS or Linux.")
        if token_url is None:
            token_url = f"https://{subdomain}.zendesk.com/oauth/tokens"
        self._subdomain = subdomain
        self._client_id = client_id
        self._http_client = http_client
        self._directory = Path(directory)
        self._token_url = token_url
        self._clock = clock
        self._lock_timeout_seconds = lock_timeout_seconds
        self._thread_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TokenStore(path={self.path!s})"

    @property
    def path(self) -> Path:
        """The token file."""
        return self._directory / TOKEN_FILE_NAME

    def load(self) -> TokenSet | None:
        """Read the token file.

        Returns:
            The stored token set, or None when there is no token file.

        Raises:
            TokenStoreError: The file is readable by others, or is not a valid token file.
            LoginRequiredError: The file is for another subdomain or client.
            OSError: The file or directory could not be read, for example `PermissionError`.
        """
        return self._read()

    def save(self, token_set: TokenSet) -> Path:
        """Write a token set atomically, under the token lock.

        Args:
            token_set: The tokens to store, for example from `request_tokens`.

        Returns:
            The path of the token file written.

        Raises:
            TokenStoreError: Timed out waiting for the token lock.
            OSError: The file could not be written. A failure before the rename leaves the previous
                file as it was. If only the directory fsync after the rename fails, the new file
                is already in place.
        """
        with self._locked():
            return self._write_locked(token_set)

    def request_tokens(
        self, grant_fields: dict[str, str], previous: TokenSet | None = None
    ) -> TokenSet:
        """POST one token request to Zendesk and return the answer. Does not save it.

        Always adds `client_id` and both lifetimes. Sends no `client_secret` (public client) and no
        `Authorization` header, even when the client has a default auth.

        Args:
            grant_fields: The grant's form fields, for example `grant_type` and `refresh_token`.
            previous: The set being refreshed, or None. Its scope is kept when the answer has none.
                A string `scope` in the answer is clipped to the previous scopes.

        Returns:
            The new token set.

        Raises:
            LoginRequiredError: Zendesk answered `invalid_grant`. On a code exchange that also
                means a bad code or redirect URI.
            TokenRefreshError: A transport error, a timeout, another error status, or a malformed
                answer.
        """
        form = {
            **grant_fields,
            "client_id": self._client_id,
            "expires_in": str(ACCESS_TOKEN_LIFETIME_SECONDS),
            "refresh_token_expires_in": str(REFRESH_TOKEN_LIFETIME_SECONDS),
        }
        response = self._post_token_form(form)
        body = _response_json(response)
        _raise_for_token_error(response.status_code, body)
        return TokenSet.from_token_response(
            body,
            subdomain=self._subdomain,
            client_id=self._client_id,
            now=self._clock(),
            previous=previous,
            requested_scope=grant_fields.get("scope", ""),
        )

    def get_access_token(self) -> str:
        """Return a usable access token, refreshing it when it is about to expire.

        Re-reads the file on every call (no lock needed: writes are atomic), so a rotation by
        another process is picked up without a 401.

        Returns:
            The access token.

        Raises:
            LoginRequiredError: The user must sign in again.
            TokenRefreshError: The refresh failed; retry later.
            TokenStoreError: The file is unusable, or the lock timed out.
            OSError: The token file or directory could not be read or written.
        """
        current = self._read_required()
        if not current.expires_soon(self._clock()):
            return current.access_token
        return self._refresh_from(current.access_token)

    def refresh_access_token(self, rejected_access_token: str) -> str:
        """Return a new access token after Zendesk rejected one with a 401.

        A refresh by another process invalidates this process's access token before it expires.
        When the file already holds a different, fresh token, that token is returned with no
        request.

        Args:
            rejected_access_token: The token Zendesk refused.

        Returns:
            The new access token.

        Raises:
            LoginRequiredError: The user must sign in again.
            TokenRefreshError: The refresh failed; retry later.
            TokenStoreError: The file is unusable, or the lock timed out.
            OSError: The token file or directory could not be read or written.
        """
        self._read_required()
        return self._refresh_from(rejected_access_token)

    def _refresh_from(self, seen_access_token: str) -> str:
        """Refresh under the lock, unless another process already did.

        Args:
            seen_access_token: The access token the caller used or found expiring.

        Returns:
            The access token to use.
        """
        with self._locked():
            current = self._read_required()
            now = self._clock()
            if current.access_token != seen_access_token and not current.expires_soon(now):
                return current.access_token
            if current.refresh_token_expired(now):
                raise LoginRequiredError(_SIGN_IN_EXPIRED_MESSAGE)
            grant_fields = {"grant_type": "refresh_token", "refresh_token": current.refresh_token}
            # Sending the stored scope keeps the granted scope stable across refreshes, and the
            # answer is clipped to it in `from_token_response`. An empty scope is left out rather
            # than sent as `scope=`.
            if current.scope:
                grant_fields["scope"] = current.scope
            new_token_set = self.request_tokens(grant_fields, previous=current)
            # Zendesk has already retired the old tokens, so save before releasing the lock.
            self._write_locked(new_token_set)
            return new_token_set.access_token

    def _post_token_form(self, form: dict[str, str]) -> httpx.Response:
        """POST the form to the token endpoint.

        The error is raised outside the `except` block, so it chains no httpx exception (whose
        request body holds the refresh token).

        Raises:
            TokenRefreshError: Any httpx error, including a timeout.
        """
        error_name = ""
        try:
            return self._http_client.post(
                self._token_url,
                data=form,
                auth=None,
                timeout=TOKEN_REQUEST_TIMEOUT_SECONDS,
                follow_redirects=False,
            )
        except httpx.HTTPError as error:
            error_name = type(error).__name__
        raise TokenRefreshError(f"Zendesk token request failed ({error_name}, unknown)")

    def _read_required(self) -> TokenSet:
        """Read the token file, raising `LoginRequiredError` when there is none."""
        current = self._read()
        if current is None:
            raise LoginRequiredError(_NO_SIGN_IN_MESSAGE)
        return current

    def _read(self) -> TokenSet | None:
        """Read and check the token file; see `load`."""
        try:
            token_fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        with os.fdopen(token_fd, "rb") as token_file:
            if os.fstat(token_file.fileno()).st_mode & 0o077:
                raise TokenStoreError(
                    f"The Zendesk token file permissions are too open; run chmod 600 {self.path}"
                )
            raw = token_file.read()
        token_set = TokenSet.from_json(_parse_json(raw))
        if token_set.subdomain != self._subdomain or token_set.client_id != self._client_id:
            raise LoginRequiredError(_OTHER_ACCOUNT_MESSAGE)
        return token_set

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold the thread lock, then `flock` on `tokens.lock`, within one time budget.

        `flock` locks belong to the open file description, so two stores in one process exclude
        each other too. The lock file is separate because `os.replace` swaps the token file's inode.

        Raises:
            TokenStoreError: The lock was not free within `lock_timeout_seconds`.
        """
        deadline = time.monotonic() + self._lock_timeout_seconds
        with contextlib.ExitStack() as cleanup:
            if not self._thread_lock.acquire(timeout=self._lock_timeout_seconds):
                raise TokenStoreError(_LOCK_TIMEOUT_MESSAGE)
            cleanup.callback(self._thread_lock.release)
            self._ensure_directory()
            lock_fd = os.open(self._directory / LOCK_FILE_NAME, os.O_RDWR | os.O_CREAT, 0o600)
            cleanup.callback(os.close, lock_fd)
            _wait_for_flock(lock_fd, deadline)
            cleanup.callback(fcntl.flock, lock_fd, fcntl.LOCK_UN)
            yield

    def _ensure_directory(self) -> Path:
        """Create the token directory if needed, and keep it at 0700."""
        os.makedirs(self._directory, mode=0o700, exist_ok=True)
        # makedirs' mode is filtered by umask and ignored for an existing directory.
        os.chmod(self._directory, 0o700)
        return self._directory

    def _write_locked(self, token_set: TokenSet) -> Path:
        """Replace the token file atomically. The caller holds the lock.

        Writes a 0600 temp file in the same directory, fsyncs it, renames it over the token file,
        then fsyncs the directory.

        Returns:
            The path of the token file written.

        Raises:
            OSError: A failure before the rename removes the temp file and keeps the old file. A
                failed directory fsync after the rename leaves the new file in place.
        """
        directory = self._ensure_directory()
        temp_fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".tokens.", suffix=".tmp")
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as temp_file:
                os.fchmod(temp_file.fileno(), 0o600)  # mkstemp already did; stated for the reader
                json.dump(token_set.to_json(), temp_file)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            os.replace(temp_path, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temp_path)
            raise
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return self.path


def _parse_json(raw: bytes) -> object:
    """Parse the token file, raising a message without its contents.

    Raises:
        TokenStoreError: The file is not valid JSON. Raised outside the `except` block, so the
            decode error (which holds the file text) is not chained.
    """
    try:
        return json.loads(raw)
    except ValueError:
        pass
    raise TokenStoreError(_UNREADABLE_FILE_MESSAGE)


def _try_flock(lock_fd: int) -> bool:
    """Take the exclusive `flock` without blocking; return False when another holder has it."""
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _wait_for_flock(lock_fd: int, deadline: float) -> bool:
    """Poll for the `flock` until `deadline` (monotonic seconds).

    Plain blocking `flock` has no timeout, and a hung holder must not hang every session.

    Returns:
        True once the lock is held.

    Raises:
        TokenStoreError: The deadline passed first.
    """
    while not _try_flock(lock_fd):
        if time.monotonic() >= deadline:
            raise TokenStoreError(_LOCK_TIMEOUT_MESSAGE)
        time.sleep(_FLOCK_POLL_SECONDS)
    return True
