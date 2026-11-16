"""Signs the stdio user in to Zendesk with OAuth and saves the tokens for the stdio server.

Run it by hand in a terminal, from the folder that holds `server.py`:

    .venv/bin/python stdio_login.py [--no-browser]

It reads `ZENDESK_SUBDOMAIN`, `ZENDESK_STDIO_OAUTH_CLIENT_ID` and `ACCESS_LEVEL` from the `.env`
next to this file, opens the Zendesk consent page (authorization code with PKCE S256 and a `state`
check), waits for the redirect on `http://127.0.0.1:8766/callback`, exchanges the code, and saves
the tokens with `stdio_oauth.TokenStore`. `readonly` asks for the `read` scope; `management` and
`admin` ask for `read write`. It then shows the signed-in user and the saved scope, which is never
wider than requested.

It runs in its own process: `server.py` never imports it, and it never imports `server` or `mcp`.
It never prints or logs a token, the authorization code, or the PKCE verifier.
"""

import argparse
import base64
import contextlib
import dataclasses
import hashlib
import math
import os
import re
import secrets
import socket
import sys
import threading
import time
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TextIO
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from dotenv import load_dotenv

from stdio_oauth import (
    DEFAULT_TOKEN_DIR,
    LoginRequiredError,
    TokenRefreshError,
    TokenSet,
    TokenStore,
    TokenStoreError,
)

# The stdio OAuth client's Identifier string (for example zendesk_mcp_stdio), not HTTP mode's
# numeric ZENDESK_OAUTH_CLIENT_ID.
CLIENT_ID_ENV_VAR = "ZENDESK_STDIO_OAUTH_CLIENT_ID"
# The redirect URI registered on the OAuth client is http://127.0.0.1:8766/callback.
CALLBACK_HOST = "127.0.0.1"
CALLBACK_PORT = 8766
CALLBACK_PATH = "/callback"
# Time to finish in the browser.
LOGIN_TIMEOUT_SECONDS = 300
# Per-connection socket timeout, so a connection that sends nothing cannot hold the listener.
CALLBACK_READ_TIMEOUT_SECONDS = 5
HTTP_TIMEOUT_SECONDS = 10
SCOPE_BY_ACCESS_LEVEL = {"readonly": "read", "management": "read write", "admin": "read write"}

# The subdomain goes into an auth-bearing host name, so it is validated.
_SUBDOMAIN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}")
_CLIENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,128}")
# An OAuth `error` value goes into a message only when it fully matches this (as in stdio_oauth).
_ERROR_CODE_PATTERN = re.compile(r"[a-z_]{1,64}")

# Static pages. They load nothing and never echo the query.
_SUCCESS_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>zendesk-mcp</title>"
    b"<p>Sign-in received. Go back to the terminal.</p>"
)
_FAILURE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>zendesk-mcp</title>"
    b"<p>Zendesk sign-in did not finish. Go back to the terminal for details.</p>"
)
_STALE_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>zendesk-mcp</title>"
    b"<p>This sign-in link is out of date. Use the newest link from the terminal.</p>"
)
_NOT_FOUND_PAGE = b"<!doctype html><meta charset=utf-8><title>zendesk-mcp</title><p>Not found.</p>"

_CODE_REFUSED_MESSAGE = (
    "Zendesk refused the sign-in code. It may have expired (codes last 120 seconds), or the "
    "redirect URI or PKCE check did not match. Run login again."
)
_ANONYMOUS_USER_MESSAGE = "Zendesk did not accept the new token (anonymous user)."


class LoginError(Exception):
    """The sign-in failed. The message is safe to print: it never holds a token or the code."""


class SettingsError(LoginError):
    """A setting in `.env` is missing or invalid."""


@dataclass(frozen=True)
class LoginSettings:
    """What the login needs from `.env`.

    Attributes:
        subdomain: The Zendesk subdomain, as written in `.env`.
        client_id: The stdio OAuth client's Identifier string.
        access_level: `readonly`, `management` or `admin`.
    """

    subdomain: str
    client_id: str
    access_level: str

    @property
    def scope(self) -> str:
        """The OAuth scope to request for the access level."""
        return SCOPE_BY_ACCESS_LEVEL[self.access_level]


def read_settings(environ: Mapping[str, str]) -> LoginSettings:
    """Read and check the login settings. Never reads `ZENDESK_EMAIL` or `ZENDESK_API_KEY`.

    Args:
        environ: The environment, for example `os.environ` after `load_dotenv`.

    Returns:
        The settings.

    Raises:
        SettingsError: A setting is missing or invalid; the message names it.
    """
    subdomain = environ.get("ZENDESK_SUBDOMAIN", "")
    if not _SUBDOMAIN_PATTERN.fullmatch(subdomain):
        raise SettingsError(
            "ZENDESK_SUBDOMAIN is missing or invalid; set it to the <subdomain> of "
            "<subdomain>.zendesk.com."
        )
    access_level = environ.get("ACCESS_LEVEL", "readonly").lower()
    if access_level not in SCOPE_BY_ACCESS_LEVEL:
        raise SettingsError("ACCESS_LEVEL must be readonly, management or admin.")
    client_id = _read_client_id(environ)
    return LoginSettings(subdomain=subdomain, client_id=client_id, access_level=access_level)


def _read_client_id(environ: Mapping[str, str]) -> str:
    """Return the checked OAuth client Identifier from `environ`.

    Raises:
        SettingsError: It is missing, has characters outside `[A-Za-z0-9_.-]`, or is all digits.
    """
    client_id = environ.get(CLIENT_ID_ENV_VAR, "")
    if not client_id:
        raise SettingsError(
            f"{CLIENT_ID_ENV_VAR} is missing. Set it to the stdio OAuth client's Identifier, "
            "from Admin Center > Apps and integrations > APIs > OAuth clients."
        )
    if not _CLIENT_ID_PATTERN.fullmatch(client_id):
        raise SettingsError(f"{CLIENT_ID_ENV_VAR} is invalid; use the OAuth client's Identifier.")
    if client_id.isdigit():
        raise SettingsError(
            f"{CLIENT_ID_ENV_VAR} looks like the numeric client ID; use the client's Identifier, "
            "for example zendesk_mcp_stdio."
        )
    return client_id


@dataclass(frozen=True)
class PkcePair:
    """A PKCE verifier and its S256 challenge. Its repr never shows the verifier."""

    verifier: str = field(repr=False)
    challenge: str


def _s256_challenge(verifier: str) -> str:
    """Return BASE64URL(SHA256(ASCII(verifier))) without padding (RFC 7636)."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def make_pkce_pair() -> PkcePair:
    """Return a new PKCE pair.

    Returns:
        An 86-character `[A-Za-z0-9_-]` verifier and its S256 challenge.
    """
    verifier = secrets.token_urlsafe(64)
    return PkcePair(verifier=verifier, challenge=_s256_challenge(verifier))


def make_state() -> str:
    """Return a new OAuth `state` value.

    Returns:
        43 random URL-safe characters (256 bits), which an attacker cannot guess.
    """
    return secrets.token_urlsafe(32)


def redirect_uri_for(port: int) -> str:
    """Return the loopback redirect URI for a bound port.

    Args:
        port: The port the callback server is bound to (8766 in production).

    Returns:
        `http://127.0.0.1:<port>/callback`.
    """
    return f"http://{CALLBACK_HOST}:{port}{CALLBACK_PATH}"


def build_authorize_url(
    settings: LoginSettings, state: str, challenge: str, redirect_uri: str
) -> str:
    """Return the Zendesk consent page URL for an authorization code with PKCE.

    The scope is written as `read%20write`, as in Zendesk's example. No `resource` parameter is
    sent: Zendesk's authorize endpoint rejects it.

    Args:
        settings: The login settings; gives the subdomain, client and scope.
        state: The `state` value the callback must return.
        challenge: The PKCE S256 challenge.
        redirect_uri: The loopback callback URI; the code exchange must send the same one.

    Returns:
        The URL.
    """
    params = {
        "response_type": "code",
        "client_id": settings.client_id,
        "redirect_uri": redirect_uri,
        "scope": settings.scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    query = urlencode(params, quote_via=quote)
    return f"https://{settings.subdomain}.zendesk.com/oauth/authorizations/new?{query}"


@dataclass(frozen=True)
class CallbackResult:
    """What a callback that ends the wait carried: a code, or an OAuth error code.

    Attributes:
        code: The authorization code, or None. Never shown in a repr.
        error: The sanitized OAuth `error` code, or None.
    """

    code: str | None = field(repr=False)
    error: str | None


@dataclass(frozen=True)
class CallbackDecision:
    """How the listener answers one callback.

    Attributes:
        status: The HTTP status to send.
        page: The static page to send.
        result: What ends the wait, or None to ignore the callback and keep waiting.
        wrong_state: True when the callback was ignored for a missing or mismatched `state`.
    """

    status: int
    page: bytes
    result: CallbackResult | None
    wrong_state: bool


def classify_callback(params: dict[str, str], expected_state: str) -> CallbackDecision:
    """Decide what one `/callback` request means. A pure function.

    In order: an `error` with a missing or matching `state` ends the login (Zendesk's denial
    redirect may carry no `state`); a missing or mismatched `state` is ignored, so an old tab
    cannot end the current login; a non-empty `code` ends the wait; anything else is ignored.

    Args:
        params: The first value of `state`, `code` and `error` from the query, when present.
        expected_state: The `state` sent on the authorize URL.

    Returns:
        The decision.
    """
    state = params.get("state")
    # Compare bytes: compare_digest raises TypeError on str with non-ASCII characters.
    state_matches = state is not None and secrets.compare_digest(
        state.encode("utf-8"), expected_state.encode("utf-8")
    )
    error = params.get("error")
    if error is not None and (state is None or state_matches):
        if not _ERROR_CODE_PATTERN.fullmatch(error):
            error = "unknown"
        return CallbackDecision(400, _FAILURE_PAGE, CallbackResult(code=None, error=error), False)
    if not state_matches:
        return CallbackDecision(400, _STALE_PAGE, None, True)
    code = params.get("code")
    if code:
        return CallbackDecision(200, _SUCCESS_PAGE, CallbackResult(code=code, error=None), False)
    return CallbackDecision(400, _FAILURE_PAGE, None, False)


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves `/callback` on the loopback listener. Logs nothing."""

    server: "_CallbackServer"

    def setup(self) -> None:
        """Set the socket timeout, and cut the connection at the login deadline.

        The read timeout (capped at the time left) restarts on every read, so a client that sends
        a byte now and then could hold the single-threaded listener past the deadline. A timer
        shuts the socket down at the deadline, which wakes a blocked read at once. There is no
        timer when no deadline is set (`wait_for_callback` sets it): `Timer(inf)` cannot run.
        """
        time_left = self.server.deadline - self.server.clock()
        self.timeout = max(0.1, min(CALLBACK_READ_TIMEOUT_SECONDS, time_left))
        super().setup()
        self._cutoff: threading.Timer | None = None
        if math.isfinite(time_left):
            self._cutoff = threading.Timer(max(0.0, time_left), self._cut_connection)
            self._cutoff.daemon = True
            self._cutoff.start()

    def finish(self) -> None:
        """Stop the deadline timer, then close the connection."""
        if self._cutoff is not None:
            self._cutoff.cancel()
        super().finish()

    def _cut_connection(self) -> None:
        """Shut the socket down (from the timer thread), so a blocked read returns at once."""
        with contextlib.suppress(OSError):
            self.connection.shutdown(socket.SHUT_RDWR)

    def log_message(self, format: str, *args: object) -> None:
        """Log nothing: the default writes the request line, which holds the code, to stderr."""
        return

    def do_GET(self) -> None:
        """Answer a callback; any other path gets 404 and the wait goes on."""
        parts = urlsplit(self.path)
        if parts.path != CALLBACK_PATH:
            self._send_page(404, _NOT_FOUND_PAGE)
            return
        query = parse_qs(parts.query, keep_blank_values=True)
        params: dict[str, str] = {}
        for name in ("state", "code", "error"):
            if name in query:
                params[name] = query[name][0]
        decision = classify_callback(params, self.server.expected_state)
        if decision.result is not None:
            self.server.result = decision.result
        if decision.wrong_state:
            self.server.saw_wrong_state = True
        self._send_page(decision.status, decision.page)

    def _send_page(self, status: int, page: bytes) -> None:
        """Send a static HTML page that is never cached and sends no referrer."""
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(len(page)))
        self.end_headers()
        self.wfile.write(page)


class _CallbackServer(HTTPServer):
    """A single-threaded loopback listener that holds the expected state and the result."""

    def __init__(self, server_address: tuple[str, int], expected_state: str) -> None:
        super().__init__(server_address, _CallbackHandler)
        self.expected_state = expected_state
        self.result: CallbackResult | None = None
        self.saw_wrong_state = False
        # `wait_for_callback` sets both; the handler uses only these, never its own clock.
        self.clock: Callable[[], float] = time.monotonic
        self.deadline = float("inf")

    def handle_error(self, request: object, client_address: object) -> None:
        """Print nothing for a failed request.

        The default prints a traceback to stderr, which would clutter the terminal during login (for
        example after a connection is cut at the deadline).
        """
        return


def bind_callback_server(
    *, expected_state: str, host: str = CALLBACK_HOST, port: int = CALLBACK_PORT
) -> _CallbackServer:
    """Start listening for the OAuth redirect.

    Args:
        expected_state: The `state` the callback must return. Required: an empty expected state
            would make an empty `state=` look like a match.
        host: The address to bind; always loopback.
        port: The port to bind; tests pass 0.

    Returns:
        The bound server. The caller must close it.

    Raises:
        LoginError: The port is in use or blocked.
    """
    try:
        return _CallbackServer((host, port), expected_state)
    except OSError:
        pass
    raise LoginError(
        f"Cannot listen on {host}:{port}: the port is in use or blocked. Close any other "
        "zendesk-mcp login and try again."
    )


def wait_for_callback(
    server: _CallbackServer, timeout_seconds: float, clock: Callable[[], float] = time.monotonic
) -> str:
    """Serve requests one at a time until a callback ends the wait or time runs out.

    Args:
        server: The bound server. The caller closes it.
        timeout_seconds: How long to wait in total.
        clock: Monotonic time in seconds.

    Returns:
        The authorization code.

    Raises:
        LoginError: Zendesk sent an error (for example the user clicked Deny), or no usable
            callback arrived in time.
    """
    server.clock = clock
    server.deadline = clock() + timeout_seconds
    while server.result is None:
        remaining = server.deadline - clock()
        if remaining <= 0:
            break
        server.timeout = min(1.0, remaining)
        server.handle_request()
    result = server.result
    if result is None:
        message = f"No sign-in received within {timeout_seconds:g} s. Run login again."
        if server.saw_wrong_state:
            message += " A callback from an out-of-date sign-in link was ignored."
        raise LoginError(message)
    if result.code is not None:
        return result.code
    message = f"Zendesk sign-in was not completed ({result.error})."
    if result.error == "access_denied":
        message += " You clicked Deny in the browser."
    raise LoginError(message)


def exchange_code(
    store: TokenStore, code: str, verifier: str, scope: str, redirect_uri: str
) -> TokenSet:
    """Exchange the authorization code for tokens. Does not save them.

    The store adds `client_id` and both lifetimes, and sends no client secret.

    Args:
        store: The token store for this subdomain and client.
        code: The authorization code from the callback.
        verifier: The PKCE verifier whose challenge was on the authorize URL.
        scope: The scope that was requested.
        redirect_uri: The same redirect URI that was on the authorize URL.

    Returns:
        The new token set.

    Raises:
        LoginError: Zendesk refused the code or the scope, or the request failed.
    """
    grant_fields = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "scope": scope,
    }
    message = ""
    try:
        return store.request_tokens(grant_fields)
    except LoginRequiredError:
        # On a code exchange, invalid_grant means a bad code, verifier or redirect URI.
        message = _CODE_REFUSED_MESSAGE
    except TokenRefreshError as error:
        message = str(error)  # The store's text is token-free.
        if "invalid_scope" in message:
            message = (
                f"The OAuth client does not allow the scope {scope}. Ask a Zendesk admin to "
                "allow it."
            )
    raise LoginError(message)


def _send_get(http_client: httpx.Client, url: str, access_token: str) -> httpx.Response:
    """Send one GET with the bearer token.

    Raises:
        LoginError: Any httpx error. Raised outside the `except` block, so it chains no httpx
            exception (whose request holds the bearer header).
    """
    error_name = ""
    try:
        return http_client.get(
            url,
            headers={"Authorization": f"Bearer {access_token}"},
            follow_redirects=False,
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        error_name = type(error).__name__
    raise LoginError(f"Request to {url} failed ({error_name}).")


def _get_with_bearer(http_client: httpx.Client, url: str, access_token: str) -> object:
    """GET a Zendesk API URL with the new access token and return the parsed JSON.

    Args:
        http_client: The client to send with.
        url: The API URL.
        access_token: The bearer token.

    Returns:
        The parsed JSON body.

    Raises:
        LoginError: The request failed, the status was not 200, or the body was not JSON.
    """
    response = _send_get(http_client, url, access_token)
    if response.status_code != 200:
        raise LoginError(f"Request to {url} returned {response.status_code}.")
    try:
        return response.json()
    except ValueError:
        pass
    raise LoginError(f"Request to {url} returned a body that is not JSON.")


def fetch_signed_in_user(
    http_client: httpx.Client, subdomain: str, access_token: str
) -> tuple[str, str]:
    """Return the signed-in user's name and email from `/api/v2/users/me.json`.

    `/users/me` answers anonymous users too, so a 200 alone does not prove the token works: the
    user must have an integer `id` and an email.

    Args:
        http_client: The client to send with.
        subdomain: The Zendesk subdomain.
        access_token: The new access token.

    Returns:
        (name, email). Nothing else from the body, which carries a CSRF token.

    Raises:
        LoginError: The request failed, or Zendesk answered for an anonymous user.
    """
    body = _get_with_bearer(
        http_client, f"https://{subdomain}.zendesk.com/api/v2/users/me.json", access_token
    )
    user = body.get("user") if isinstance(body, dict) else None
    if not _is_signed_in_user(user):
        raise LoginError(_ANONYMOUS_USER_MESSAGE)
    return str(user.get("name") or ""), user["email"]


def _is_signed_in_user(user: object) -> bool:
    """Return True for a user object with an integer (not bool) `id` and a non-empty `email`."""
    return (
        isinstance(user, dict)
        and type(user.get("id")) is int
        and isinstance(user.get("email"), str)
        and user["email"] != ""
    )


def fetch_granted_scopes(
    http_client: httpx.Client, subdomain: str, access_token: str
) -> frozenset[str]:
    """Return the scopes Zendesk records for the token, from `/api/v2/oauth/tokens/current.json`.

    This is Zendesk's own record. The token answer may have no `scope`, and then the saved scope
    is only the requested one. `run_login` clips the saved scope to this result.

    Args:
        http_client: The client to send with.
        subdomain: The Zendesk subdomain.
        access_token: The new access token.

    Returns:
        The granted scopes. The saved scope is clipped to them.

    Raises:
        LoginError: The request failed, or the answer has no `token.scopes` list.
    """
    body = _get_with_bearer(
        http_client,
        f"https://{subdomain}.zendesk.com/api/v2/oauth/tokens/current.json",
        access_token,
    )
    token = body.get("token") if isinstance(body, dict) else None
    scopes = token.get("scopes") if isinstance(token, dict) else None
    if not isinstance(scopes, list):
        raise LoginError("Zendesk returned an unexpected answer for the token check.")
    return frozenset(str(scope) for scope in scopes)


def format_report(
    settings: LoginSettings, token_path: Path, name: str, email: str, saved_scopes: frozenset[str]
) -> list[str]:
    """Return the lines that report a finished login. They never hold a token or the code.

    Args:
        settings: The login settings.
        token_path: Where the tokens were saved.
        name: The signed-in user's name.
        email: The signed-in user's email.
        saved_scopes: The saved scopes: requested, in the token answer, and granted by Zendesk.

    Returns:
        The lines to print, with a warning last when the saved scope is narrower than requested.
    """
    saved_text = " ".join(sorted(saved_scopes))
    lines = [
        f"Signed in to {settings.subdomain}.zendesk.com as {name} <{email}>.",
        f"Saved scope: {saved_text} (requested: {settings.scope}; "
        + f"ACCESS_LEVEL={settings.access_level}).",
        f"Tokens saved to {token_path}.",
    ]
    missing = frozenset(settings.scope.split()) - saved_scopes
    if missing:
        lines.append(
            "Warning: The saved scope is narrower than requested; tools that need "
            f"{' '.join(sorted(missing))} will not work."
        )
    return lines


def _open_browser_or_report(
    open_browser: Callable[[str], bool] | None, url: str, out: TextIO
) -> bool:
    """Try to open the URL; print the fallback line when no browser opened.

    Args:
        open_browser: Opens a URL, or None for `--no-browser` (only the URL is shown).
        url: The consent page URL.
        out: Where to print.

    Returns:
        True when the browser opened.
    """
    if open_browser is None:
        print("Open the URL above in your browser.", file=out)
        return False
    try:
        opened = open_browser(url)
    except webbrowser.Error:
        opened = False
    if not opened:
        print("Could not open a browser; open the URL above.", file=out)
    return opened


def _receive_code(
    settings: LoginSettings,
    challenge: str,
    *,
    open_browser: Callable[[str], bool] | None,
    port: int,
    timeout_seconds: float,
    out: TextIO,
) -> tuple[str, str]:
    """Bind the listener, show the consent URL, and wait for the code. Always frees the port.

    Returns:
        (code, redirect_uri): the redirect URI is the one on the URL, for the code exchange.

    Raises:
        LoginError: The port is busy, Zendesk sent an error, or no code arrived in time.
    """
    state = make_state()
    server = bind_callback_server(port=port, expected_state=state)
    try:
        redirect_uri = redirect_uri_for(server.server_address[1])
        url = build_authorize_url(settings, state, challenge, redirect_uri)
        # The URL holds the state and the challenge, never the verifier or a code.
        print("Open this URL to sign in to Zendesk:", file=out)
        print(url, file=out, flush=True)
        _open_browser_or_report(open_browser, url, out)
        print(f"Waiting up to {timeout_seconds:g} s for the sign-in...", file=out, flush=True)
        code = wait_for_callback(server, timeout_seconds)
    finally:
        server.server_close()
    return code, redirect_uri


def run_login(
    settings: LoginSettings,
    *,
    http_client: httpx.Client,
    token_dir: Path = DEFAULT_TOKEN_DIR,
    open_browser: Callable[[str], bool] | None = webbrowser.open,
    port: int = CALLBACK_PORT,
    timeout_seconds: float = LOGIN_TIMEOUT_SECONDS,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    """Run the whole sign-in and save the tokens.

    The token store and the callback port fail before the browser opens. An unwritable token
    directory or a lock timeout fails at the save, after Zendesk has used the code; the user fixes
    it and runs login again. The tokens are saved as soon as the exchange succeeds, because Zendesk
    has used up the code. The saved scope is clipped to the requested scope before the first save,
    and to Zendesk's record of the token afterwards; it is never wider than requested.

    Args:
        settings: The login settings.
        http_client: The client for the token request and the checks.
        token_dir: Where `tokens.json` goes.
        open_browser: Opens a URL and returns False when no browser opened; None only prints the
            URL (`--no-browser`).
        port: The callback port; tests pass 0.
        timeout_seconds: How long to wait for the browser.
        out: Where to print; None means `sys.stdout` at call time.
        err: Where to print the check-failed error; None means `sys.stderr` at call time.

    Returns:
        0 on success, 1 when the tokens were saved but checking them failed.

    Raises:
        LoginError: The sign-in failed before the tokens were saved.
        TokenStoreError: The token store cannot run here, or its lock timed out.
        OSError: The token file could not be written.
    """
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    store = TokenStore(
        subdomain=settings.subdomain,
        client_id=settings.client_id,
        http_client=http_client,
        directory=token_dir,
    )
    pkce = make_pkce_pair()
    code, redirect_uri = _receive_code(
        settings,
        pkce.challenge,
        open_browser=open_browser,
        port=port,
        timeout_seconds=timeout_seconds,
        out=out,
    )
    token_set = exchange_code(store, code, pkce.verifier, settings.scope, redirect_uri)
    # Clip before the first save, so no later failure can leave a wider scope on disk.
    token_set = _clip_scope(token_set, frozenset(settings.scope.split()))
    token_path = store.save(token_set)
    try:
        name, email = fetch_signed_in_user(http_client, settings.subdomain, token_set.access_token)
        granted = fetch_granted_scopes(http_client, settings.subdomain, token_set.access_token)
    except LoginError as error:
        message = f"error: Tokens were saved to {token_path}, but checking them failed: {error}"
        print(message, file=err)
        return 1
    clipped = _clip_scope(token_set, granted)
    if clipped.scopes != token_set.scopes:
        store.save(clipped)
    for line in format_report(settings, token_path, name, email, clipped.scopes):
        print(line, file=out)
    return 0


def _clip_scope(token_set: TokenSet, allowed: frozenset[str]) -> TokenSet:
    """Return the token set with its scope cut down to the scopes in `allowed`.

    Args:
        token_set: The token set to clip.
        allowed: The scopes it may keep.

    Returns:
        A copy whose scope is the sorted, space-joined overlap.
    """
    return dataclasses.replace(token_set, scope=" ".join(sorted(token_set.scopes & allowed)))


def _login_from_env(open_browser: Callable[[str], bool] | None) -> int:
    """Load `.env`, read the settings, and run the login with a short-lived HTTP client."""
    load_dotenv(Path(__file__).parent / ".env")
    settings = read_settings(os.environ)
    # This is its own process, not the stdio server, so it builds its own client.
    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=False) as http_client:
        return run_login(settings, http_client=http_client, open_browser=open_browser)


def main(argv: list[str] | None = None) -> int:
    """Run the login command.

    Args:
        argv: The command-line arguments, without the program name; None means `sys.argv[1:]`.

    Returns:
        The exit code: 0 signed in, 1 failed, 2 bad settings in `.env`, 130 cancelled.
    """
    parser = argparse.ArgumentParser(
        prog="stdio_login.py", description="Sign the stdio zendesk-mcp server in to Zendesk."
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="do not open a browser; only print the sign-in URL",
    )
    args = parser.parse_args(argv)
    open_browser = None if args.no_browser else webbrowser.open
    try:
        return _login_from_env(open_browser)
    except SettingsError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (LoginError, TokenStoreError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
