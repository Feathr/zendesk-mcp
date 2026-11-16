"""Offline tests for the stdio sign-in command in `stdio_login.py`.

Zendesk is stubbed with an `httpx.MockTransport`. The loopback listener is real: it binds
127.0.0.1 on port 0, and helper threads play the browser with `httpx.Client(trust_env=False)`, so
proxy env vars cannot reroute the loopback calls. Token dirs live under `tmp_path`, and no test
reads `.env` (`main` tests patch `load_dotenv`).
"""

import ast
import io
import itertools
import re
import socket
import stat
import threading
import time
import webbrowser
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

import stdio_login
import stdio_oauth
from stdio_login import (
    _FAILURE_PAGE,
    _STALE_PAGE,
    _SUCCESS_PAGE,
    CallbackDecision,
    CallbackResult,
    LoginError,
    LoginSettings,
    SettingsError,
    _get_with_bearer,
    _s256_challenge,
    bind_callback_server,
    build_authorize_url,
    classify_callback,
    exchange_code,
    fetch_granted_scopes,
    fetch_signed_in_user,
    format_report,
    main,
    make_pkce_pair,
    make_state,
    read_settings,
    redirect_uri_for,
    run_login,
    wait_for_callback,
)
from stdio_oauth import TokenStore, TokenStoreError

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBDOMAIN = "example"
CLIENT_ID = "zendesk_mcp_stdio"
STATE = "expected-state"
GOOD_CODE = "code-login"
USERS_ME_PATH = "/api/v2/users/me.json"
TOKENS_CURRENT_PATH = "/api/v2/oauth/tokens/current.json"
# Every value a leak would show: the issued tokens, their 10-character prefixes from
# tokens/current, the authorization code, and the CSRF token in the users/me body.
SECRETS = ("access-login", "refresh-login", "access-log", "refresh-lo", GOOD_CODE, "csrf-secret")
USER_BODY = {
    "user": {
        "id": 5,
        "name": "Ada Agent",
        "email": "ada@example.com",
        "role": "agent",
        "authenticity_token": "csrf-secret",
    }
}
GOOD_ENV = {"ZENDESK_SUBDOMAIN": SUBDOMAIN, "ZENDESK_STDIO_OAUTH_CLIENT_ID": CLIENT_ID}


class ZendeskStub:
    """Stub Zendesk for `/oauth/tokens`, `users/me` and `tokens/current`.

    The token route echoes the requested scope, or answers `token_answer_scope` when it is set, or
    sends no `scope` when `scope_in_token_answer` is False.
    `tokens/current` reports `granted_scopes`, or the requested scopes when it is None. A test puts
    `(status, body)` in `overrides[path]` to change one route; a `bytes` body is sent as is.
    """

    def __init__(self, granted_scopes: list[str] | None = None) -> None:
        self.granted_scopes = granted_scopes
        self.scope_in_token_answer = True
        self.token_answer_scope: str | None = None
        self.overrides: dict[str, tuple[int, object]] = {}
        self.token_requests: list[httpx.Request] = []
        self.get_requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            self.token_requests.append(request)
        else:
            self.get_requests.append(request)
        if path in self.overrides:
            status, body = self.overrides[path]
            if isinstance(body, bytes):
                return httpx.Response(status, content=body)
            return httpx.Response(status, json=body)
        if path == "/oauth/tokens":
            return httpx.Response(201, json=self._token_answer(form_fields(request)))
        if path == USERS_ME_PATH:
            return httpx.Response(200, json=USER_BODY)
        if path == TOKENS_CURRENT_PATH:
            return httpx.Response(200, json=self._current_answer())
        return httpx.Response(404, json={"error": "not_found"})

    def _token_answer(self, form: dict[str, str]) -> dict[str, object]:
        body: dict[str, object] = {
            "access_token": "access-login",
            "refresh_token": "refresh-login",
            "token_type": "bearer",
            "expires_in": 3600,
            "refresh_token_expires_in": 7_776_000,
        }
        if self.scope_in_token_answer and self.token_answer_scope is not None:
            body["scope"] = self.token_answer_scope
        elif self.scope_in_token_answer:
            body["scope"] = form.get("scope", "")
        return body

    def _current_answer(self) -> dict[str, object]:
        scopes = self.granted_scopes
        if scopes is None:
            scopes = form_fields(self.token_requests[-1]).get("scope", "").split()
        return {
            "token": {
                "scopes": scopes,
                "client_id": 9,
                "user_id": 5,
                "token": "access-log",
                "refresh_token": "refresh-lo",
            }
        }


def form_fields(request: httpx.Request) -> dict[str, str]:
    """Return the form-encoded body of a recorded request as a plain dict."""
    return dict(parse_qsl(request.content.decode()))


def make_client(stub: ZendeskStub) -> httpx.Client:
    """Return an `httpx.Client` whose requests all go to `stub`."""
    return httpx.Client(transport=httpx.MockTransport(stub), trust_env=False)


def make_store(token_dir: Path, stub: ZendeskStub) -> TokenStore:
    """Return a token store under `token_dir` that talks to `stub`."""
    return TokenStore(
        subdomain=SUBDOMAIN, client_id=CLIENT_ID, http_client=make_client(stub), directory=token_dir
    )


def authorize_params(url: str) -> dict[str, str]:
    """Return the query parameters of an authorize URL."""
    return dict(parse_qsl(urlsplit(url).query))


def statuses(outcomes: list[httpx.Response | str]) -> list[int | str]:
    """Return each outcome's status code, or the error name when the request failed."""
    result: list[int | str] = []
    for outcome in outcomes:
        if isinstance(outcome, httpx.Response):
            result.append(outcome.status_code)
        else:
            result.append(outcome)
    return result


def get_in_thread(
    urls: list[str], *, delay_seconds: float = 0.0
) -> tuple[threading.Thread, list[httpx.Response | str]]:
    """GET each URL in turn from a daemon thread; record each response or error name.

    A thread is needed because `wait_for_callback` serves requests on the calling thread.
    """
    outcomes: list[httpx.Response | str] = []

    def send() -> None:
        time.sleep(delay_seconds)
        with httpx.Client(trust_env=False, timeout=3.0) as client:
            for url in urls:
                try:
                    outcomes.append(client.get(url))
                except httpx.HTTPError as error:
                    outcomes.append(type(error).__name__)

    thread = threading.Thread(target=send, daemon=True)
    thread.start()
    return thread, outcomes


class SilentConnection:
    """Opens a TCP connection from a daemon thread, sends nothing, and holds it until released."""

    def __init__(self, port: int) -> None:
        self.connected = threading.Event()
        self.error: str | None = None
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._hold, args=(port,), daemon=True)
        self._thread.start()
        self.connected.wait(5)

    def _hold(self, port: int) -> None:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError as error:
            self.error = type(error).__name__
            self.connected.set()
            return
        self.connected.set()
        self._released.wait(10)
        connection.close()

    def release(self) -> None:
        self._released.set()
        self._thread.join(5)


class FakeBrowser:
    """Plays the browser for `run_login`.

    It reads `redirect_uri` (which carries the bound port) and `state` from the authorize URL, then
    a thread GETs `/favicon.ico` (expects 404) and the callback with `code-login`. The opener then
    returns `opened`, or raises `raises`.
    """

    def __init__(self, *, opened: bool = True, raises: Exception | None = None) -> None:
        self.opened = opened
        self.raises = raises
        self.url: str | None = None
        self.outcomes: list[httpx.Response | str] = []
        self._thread: threading.Thread | None = None

    def __call__(self, url: str) -> bool:
        self.url = url
        params = authorize_params(url)
        redirect_uri = params["redirect_uri"]
        origin = redirect_uri.removesuffix("/callback")
        callback = f"{redirect_uri}?code={GOOD_CODE}&state={params['state']}"
        self._thread, self.outcomes = get_in_thread([f"{origin}/favicon.ico", callback])
        if self.raises is not None:
            raise self.raises
        return self.opened

    def join(self) -> None:
        if self._thread is not None:
            self._thread.join(5)


def run_flow(
    tmp_path: Path, stub: ZendeskStub, access_level: str, browser: FakeBrowser | None = None
) -> tuple[int, FakeBrowser, Path]:
    """Run the whole login against `stub` and a fake browser; return (exit code, browser, dir)."""
    if browser is None:
        browser = FakeBrowser()
    token_dir = tmp_path / "cfg"
    exit_code = run_login(
        LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level=access_level),
        http_client=make_client(stub),
        token_dir=token_dir,
        open_browser=browser,
        port=0,
        timeout_seconds=10,
    )
    browser.join()
    return exit_code, browser, token_dir


class TrickleConnection:
    """Sends one byte of a request line every `interval` s from a daemon thread, never a newline.

    It stops after `duration` s, on `release()`, or when a send fails because the listener cut the
    connection.
    """

    def __init__(self, port: int, *, interval: float = 0.3, duration: float = 6.0) -> None:
        self.connected = threading.Event()
        self.error: str | None = None
        self._interval = interval
        self._duration = duration
        self._released = threading.Event()
        self._thread = threading.Thread(target=self._send, args=(port,), daemon=True)
        self._thread.start()
        self.connected.wait(5)

    def _send(self, port: int) -> None:
        try:
            connection = socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError as error:
            self.error = type(error).__name__
            self.connected.set()
            return
        self.connected.set()
        stop_at = time.monotonic() + self._duration
        with connection:
            for byte in itertools.cycle(b"GET /callback HTTP/1.0"):
                if time.monotonic() >= stop_at or self._released.is_set():
                    return
                try:
                    connection.sendall(bytes([byte]))
                except OSError:
                    return
                self._released.wait(self._interval)

    def release(self) -> None:
        self._released.set()
        self._thread.join(8)


class CallbackOnUrl(io.StringIO):
    """An output stream that sends the callback from a thread once the authorize URL is written.

    It stands in for the user who copies the printed URL when no browser opener runs.
    """

    def __init__(self) -> None:
        super().__init__()
        self.thread: threading.Thread | None = None
        self.outcomes: list[httpx.Response | str] = []

    def write(self, text: str) -> int:
        if self.thread is None and "/oauth/authorizations/new?" in text:
            params = authorize_params(text.strip())
            callback = f"{params['redirect_uri']}?code={GOOD_CODE}&state={params['state']}"
            self.thread, self.outcomes = get_in_thread([callback])
        return super().write(text)


def outcome_of(call: Callable[[], int]) -> int | str:
    """Return the call's result, or the name of the exception it raised, so a crash fails an assert."""
    try:
        return call()
    except (TypeError, OSError, LoginError, TokenStoreError) as error:
        return type(error).__name__


def record_saves(monkeypatch: pytest.MonkeyPatch, fail_on_call: int | None = None) -> list[str]:
    """Wrap `TokenStore.save` to record each saved scope; raise `OSError` on call `fail_on_call`."""
    saved_scopes: list[str] = []
    real_save = TokenStore.save

    def recording_save(self: TokenStore, token_set: stdio_oauth.TokenSet) -> Path:
        saved_scopes.append(token_set.scope)
        if len(saved_scopes) == fail_on_call:
            raise OSError("disk full")
        return real_save(self, token_set)

    monkeypatch.setattr(TokenStore, "save", recording_save)
    return saved_scopes


def saved_scope(token_dir: Path, stub: ZendeskStub) -> str | None:
    """Return the scope in the token file, or None when there is no file."""
    loaded = make_store(token_dir, stub).load()
    if loaded is None:
        return None
    return loaded.scope


def imported_modules(path: Path) -> set[str]:
    """Return every module name that a file imports."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            names.add(node.module)
    return names


def live_connection_timers(server: stdio_login._CallbackServer) -> list[threading.Thread]:
    """Return the running per-connection deadline timers of handlers on `server`."""
    timers: list[threading.Thread] = []
    for running in threading.enumerate():
        if not isinstance(running, threading.Timer) or not running.is_alive():
            continue
        handler = getattr(running.function, "__self__", None)
        if getattr(handler, "server", None) is server:
            timers.append(running)
    return timers


@pytest.fixture
def listener() -> Iterator[stdio_login._CallbackServer]:
    """A callback server on 127.0.0.1, port 0, expecting `STATE`."""
    server = bind_callback_server(port=0, expected_state=STATE)
    yield server
    server.server_close()


def callback_url(server: stdio_login._CallbackServer, query: str) -> str:
    """Return the callback URL on the server's bound port with `query`."""
    return f"http://127.0.0.1:{server.server_address[1]}/callback?{query}"


# PKCE, state and the authorize URL.


def test_make_pkce_pair_matches_rfc7636_vector() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert _s256_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_make_pkce_pair_verifier_length_and_charset() -> None:
    first = make_pkce_pair()
    second = make_pkce_pair()

    assert 43 <= len(first.verifier) <= 128
    assert re.fullmatch(r"[A-Za-z0-9_-]+", first.verifier)
    assert first.verifier != second.verifier
    assert first.challenge == _s256_challenge(first.verifier)
    assert first.verifier not in repr(first)
    state = make_state()
    assert len(state) >= 43
    assert state != make_state()


def test_build_authorize_url_params() -> None:
    settings = LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level="management")
    redirect_uri = "http://127.0.0.1:8766/callback"

    url = build_authorize_url(settings, "state-1", "challenge-1", redirect_uri)

    parts = urlsplit(url)
    assert parts.scheme == "https"
    assert parts.hostname == "example.zendesk.com"
    assert parts.path == "/oauth/authorizations/new"
    assert parse_qsl(parts.query) == [
        ("response_type", "code"),
        ("client_id", CLIENT_ID),
        ("redirect_uri", redirect_uri),
        ("scope", "read write"),
        ("state", "state-1"),
        ("code_challenge", "challenge-1"),
        ("code_challenge_method", "S256"),
    ]
    assert "scope=read%20write" in parts.query
    assert "resource" not in parts.query


# Settings.


@pytest.mark.parametrize(
    ("access_level", "expected_level", "expected_scope"),
    [
        (None, "readonly", "read"),
        ("readonly", "readonly", "read"),
        ("management", "management", "read write"),
        ("admin", "admin", "read write"),
        ("ADMIN", "admin", "read write"),
    ],
)
def test_read_settings_scope_by_access_level(
    access_level: str | None, expected_level: str, expected_scope: str
) -> None:
    environ = dict(GOOD_ENV)
    if access_level is not None:
        environ["ACCESS_LEVEL"] = access_level

    settings = read_settings(environ)

    assert settings.access_level == expected_level
    assert settings.scope == expected_scope


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"ZENDESK_SUBDOMAIN": None}, "ZENDESK_SUBDOMAIN"),
        ({"ZENDESK_SUBDOMAIN": "exa mple"}, "ZENDESK_SUBDOMAIN"),
        ({"ZENDESK_STDIO_OAUTH_CLIENT_ID": None}, "ZENDESK_STDIO_OAUTH_CLIENT_ID.*OAuth clients"),
        (
            {"ZENDESK_STDIO_OAUTH_CLIENT_ID": "44928675177239"},
            "ZENDESK_STDIO_OAUTH_CLIENT_ID.*numeric",
        ),
        ({"ZENDESK_STDIO_OAUTH_CLIENT_ID": "zendesk mcp"}, "ZENDESK_STDIO_OAUTH_CLIENT_ID"),
        ({"ACCESS_LEVEL": "superuser"}, "ACCESS_LEVEL"),
    ],
    ids=[
        "missing-subdomain",
        "bad-subdomain",
        "missing-client-id",
        "numeric-client-id",
        "bad-client-id",
        "bad-access-level",
    ],
)
def test_read_settings_rejects_bad_values(overrides: dict[str, str | None], message: str) -> None:
    environ = dict(GOOD_ENV)
    for name, value in overrides.items():
        if value is None:
            environ.pop(name, None)
        else:
            environ[name] = value

    with pytest.raises(SettingsError, match=message):
        read_settings(environ)


def test_read_settings_needs_no_api_token() -> None:
    # No ZENDESK_EMAIL or ZENDESK_API_KEY. The subdomain is kept as written, as server.py does.
    environ = {"ZENDESK_SUBDOMAIN": "Example", "ZENDESK_STDIO_OAUTH_CLIENT_ID": CLIENT_ID}

    settings = read_settings(environ)

    assert settings == LoginSettings(
        subdomain="Example", client_id=CLIENT_ID, access_level="readonly"
    )


def test_login_hint_names_stdio_login() -> None:
    assert "stdio_login.py" in stdio_oauth.LOGIN_HINT
    assert (REPO_ROOT / "stdio_login.py").is_file()
    assert (REPO_ROOT / "server.py").is_file()


# The callback.


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        (
            {"state": STATE, "code": GOOD_CODE},
            CallbackDecision(200, _SUCCESS_PAGE, CallbackResult(code=GOOD_CODE, error=None), False),
        ),
        ({"state": "old-state", "code": GOOD_CODE}, CallbackDecision(400, _STALE_PAGE, None, True)),
        ({"code": GOOD_CODE}, CallbackDecision(400, _STALE_PAGE, None, True)),
        (
            {"error": "access_denied"},
            CallbackDecision(
                400, _FAILURE_PAGE, CallbackResult(code=None, error="access_denied"), False
            ),
        ),
        (
            {"error": "access_denied", "state": STATE},
            CallbackDecision(
                400, _FAILURE_PAGE, CallbackResult(code=None, error="access_denied"), False
            ),
        ),
        (
            {"error": "access_denied", "state": "old-state"},
            CallbackDecision(400, _STALE_PAGE, None, True),
        ),
        (
            {"error": "x y<script>"},
            CallbackDecision(400, _FAILURE_PAGE, CallbackResult(code=None, error="unknown"), False),
        ),
        ({"state": STATE, "code": ""}, CallbackDecision(400, _FAILURE_PAGE, None, False)),
        ({"state": "é", "code": GOOD_CODE}, CallbackDecision(400, _STALE_PAGE, None, True)),
        ({"state": "", "code": GOOD_CODE}, CallbackDecision(400, _STALE_PAGE, None, True)),
    ],
    ids=[
        "good-code",
        "wrong-state",
        "missing-state",
        "error-no-state",
        "error-matching-state",
        "error-wrong-state",
        "junk-error",
        "empty-code",
        "non-ascii-state",
        "blank-state",
    ],
)
def test_classify_callback_cases(params: dict[str, str], expected: CallbackDecision) -> None:
    assert classify_callback(params, STATE) == expected


def test_bind_callback_server_binds_loopback_only() -> None:
    server = bind_callback_server(port=0, expected_state=STATE)
    try:
        port = server.server_address[1]
        assert server.server_address[0] == "127.0.0.1"
        assert redirect_uri_for(port) == f"http://127.0.0.1:{port}/callback"
    finally:
        server.server_close()


def test_bind_callback_server_port_in_use() -> None:
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]

        with pytest.raises(LoginError, match=f"127.0.0.1:{port}") as raised:
            bind_callback_server(port=port, expected_state=STATE)

    assert raised.value.__context__ is None


def test_bind_callback_server_requires_expected_state() -> None:
    with pytest.raises(TypeError):
        bind_callback_server(port=0)  # type: ignore[call-arg]


def test_wait_for_callback_ignores_other_paths(listener: stdio_login._CallbackServer) -> None:
    port = listener.server_address[1]
    thread, outcomes = get_in_thread(
        [
            f"http://127.0.0.1:{port}/favicon.ico",
            callback_url(listener, f"code={GOOD_CODE}&state={STATE}"),
        ]
    )

    code = wait_for_callback(listener, timeout_seconds=5)
    thread.join(5)

    assert code == GOOD_CODE
    assert statuses(outcomes) == [404, 200]


def test_wait_for_callback_ignores_wrong_state(listener: stdio_login._CallbackServer) -> None:
    thread, outcomes = get_in_thread([callback_url(listener, f"code={GOOD_CODE}&state=old-state")])

    with pytest.raises(LoginError, match="out-of-date") as raised:
        wait_for_callback(listener, timeout_seconds=3)
    thread.join(5)

    assert statuses(outcomes) == [400]
    assert listener.result is None
    assert GOOD_CODE not in str(raised.value)


def test_wait_for_callback_error_with_blank_state_is_ignored(
    listener: stdio_login._CallbackServer,
) -> None:
    # Without keep_blank_values the blank `state` would read as missing, and the error would end it.
    thread, outcomes = get_in_thread([callback_url(listener, "error=access_denied&state=")])

    with pytest.raises(LoginError, match="No sign-in received") as raised:
        wait_for_callback(listener, timeout_seconds=2)
    thread.join(5)

    assert "out-of-date" in str(raised.value)
    assert "access_denied" not in str(raised.value)
    assert statuses(outcomes) == [400]
    assert listener.saw_wrong_state


@pytest.mark.parametrize(
    ("error_code", "says_deny"), [("access_denied", True), ("server_error", False)]
)
def test_wait_for_callback_error_without_state_ends_login(
    listener: stdio_login._CallbackServer, error_code: str, says_deny: bool
) -> None:
    query = f"error={error_code}&error_description=Secret+description+text"
    thread, outcomes = get_in_thread([callback_url(listener, query)])
    started = time.monotonic()

    with pytest.raises(LoginError, match=error_code) as raised:
        wait_for_callback(listener, timeout_seconds=5)
    elapsed = time.monotonic() - started
    thread.join(5)

    message = str(raised.value)
    assert ("Deny" in message) is says_deny
    assert "Secret" not in message
    assert elapsed < 3
    assert statuses(outcomes) == [400]


def test_wait_for_callback_times_out(listener: stdio_login._CallbackServer) -> None:
    started = time.monotonic()

    with pytest.raises(LoginError, match="No sign-in received within 0.5 s") as raised:
        wait_for_callback(listener, timeout_seconds=0.5)

    assert time.monotonic() - started < 2
    assert "out-of-date" not in str(raised.value)


def test_wait_for_callback_silent_connection_does_not_block(
    listener: stdio_login._CallbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stdio_login, "CALLBACK_READ_TIMEOUT_SECONDS", 0.5)
    silent = SilentConnection(listener.server_address[1])
    thread, outcomes = get_in_thread(
        [callback_url(listener, f"code={GOOD_CODE}&state={STATE}")], delay_seconds=0.1
    )
    started = time.monotonic()

    code = wait_for_callback(listener, timeout_seconds=10)
    elapsed = time.monotonic() - started
    silent.release()
    thread.join(5)

    assert code == GOOD_CODE
    assert silent.error is None
    assert elapsed < 3
    assert statuses(outcomes) == [200]


def test_wait_for_callback_silent_connection_respects_deadline(
    listener: stdio_login._CallbackServer,
) -> None:
    # The read timeout stays at 5 s; only the clamp to the time left ends this within ~1 s.
    silent = SilentConnection(listener.server_address[1])
    started = time.monotonic()

    with pytest.raises(LoginError, match="No sign-in received"):
        wait_for_callback(listener, timeout_seconds=1)
    elapsed = time.monotonic() - started
    silent.release()

    assert silent.error is None
    assert elapsed < 2.5


def test_wait_for_callback_trickling_connection_respects_deadline(
    listener: stdio_login._CallbackServer, capfd: pytest.CaptureFixture[str]
) -> None:
    # The read timeout stays at 5 s and each byte restarts it; only the connection timer ends this.
    trickle = TrickleConnection(listener.server_address[1])
    started = time.monotonic()

    with pytest.raises(LoginError, match="No sign-in received"):
        wait_for_callback(listener, timeout_seconds=1)
    elapsed = time.monotonic() - started
    trickle.release()

    assert trickle.error is None
    assert elapsed < 2.5
    assert capfd.readouterr().err == ""


def test_callback_server_handle_error_prints_nothing(
    listener: stdio_login._CallbackServer,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    def broken_classify(params: dict[str, str], expected_state: str) -> CallbackDecision:
        raise RuntimeError("handler bug")

    monkeypatch.setattr(stdio_login, "classify_callback", broken_classify)
    thread, outcomes = get_in_thread([callback_url(listener, f"code={GOOD_CODE}&state={STATE}")])

    with pytest.raises(LoginError, match="No sign-in received"):
        wait_for_callback(listener, timeout_seconds=2)
    thread.join(5)

    # The handler ran and failed (no answer was sent), and nothing reached the terminal.
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], str)
    assert capfd.readouterr().err == ""
    monkeypatch.undo()
    thread, outcomes = get_in_thread([callback_url(listener, f"code={GOOD_CODE}&state={STATE}")])
    assert wait_for_callback(listener, timeout_seconds=5) == GOOD_CODE
    thread.join(5)


def test_callback_handler_timeout_floor(
    listener: stdio_login._CallbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied_timeouts: list[float | None] = []
    real_setup = stdio_login._CallbackHandler.setup

    def recording_setup(self: stdio_login._CallbackHandler) -> None:
        real_setup(self)
        applied_timeouts.append(self.timeout)

    monkeypatch.setattr(stdio_login._CallbackHandler, "setup", recording_setup)
    listener.deadline = listener.clock() + 0.01
    silent = SilentConnection(listener.server_address[1])
    started = time.monotonic()

    listener.handle_request()
    elapsed = time.monotonic() - started
    silent.release()

    # The recorded timeout proves the floor; the timing alone would not (the timer cuts anyway).
    assert silent.error is None
    assert applied_timeouts == [0.1]
    assert elapsed < 1


def test_callback_handler_without_deadline_starts_no_timer(
    listener: stdio_login._CallbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A direct handle_request() has no login deadline; Timer(inf) would raise in its thread.
    thread_errors: list[str] = []

    def record_thread_error(args: threading.ExceptHookArgs) -> None:
        thread_errors.append(args.exc_type.__name__)

    monkeypatch.setattr(threading, "excepthook", record_thread_error)
    thread, outcomes = get_in_thread([callback_url(listener, f"code={GOOD_CODE}&state={STATE}")])

    listener.handle_request()
    thread.join(5)
    time.sleep(0.2)

    assert statuses(outcomes) == [200]
    assert listener.result == CallbackResult(code=GOOD_CODE, error=None)
    assert thread_errors == []


def test_callback_handler_cleans_up_connection_timers(
    listener: stdio_login._CallbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    # pytest's capfd cannot see exceptions in other threads, so record them with excepthook.
    thread_errors: list[str] = []

    def record_thread_error(args: threading.ExceptHookArgs) -> None:
        thread_errors.append(args.exc_type.__name__)

    monkeypatch.setattr(threading, "excepthook", record_thread_error)
    port = listener.server_address[1]
    thread, outcomes = get_in_thread(
        [
            f"http://127.0.0.1:{port}/favicon.ico",
            callback_url(listener, f"code={GOOD_CODE}&state=old-state"),
            callback_url(listener, f"code={GOOD_CODE}&state={STATE}"),
        ]
    )

    code = wait_for_callback(listener, timeout_seconds=5)
    thread.join(5)

    # Each finished connection cancelled its timer, long before the 5 s deadline.
    assert code == GOOD_CODE
    assert statuses(outcomes) == [404, 400, 200]
    assert live_connection_timers(listener) == []
    # A timer that fires just after `finish` closed the socket must not raise in its thread.
    handler = stdio_login._CallbackHandler.__new__(stdio_login._CallbackHandler)
    handler.connection = socket.socket()
    handler.connection.close()
    handler._cut_connection()
    time.sleep(0.2)
    assert thread_errors == []


def test_callback_result_repr_hides_code() -> None:
    assert GOOD_CODE not in repr(CallbackResult(code=GOOD_CODE, error=None))


def test_callback_handler_logs_nothing(
    listener: stdio_login._CallbackServer, capfd: pytest.CaptureFixture[str]
) -> None:
    thread, outcomes = get_in_thread([callback_url(listener, f"code={GOOD_CODE}&state={STATE}")])

    code = wait_for_callback(listener, timeout_seconds=5)
    thread.join(5)

    assert code == GOOD_CODE
    assert len(outcomes) == 1
    response = outcomes[0]
    assert isinstance(response, httpx.Response)
    assert response.status_code == 200
    assert b"Sign-in received" in response.content
    assert GOOD_CODE.encode() not in response.content
    assert STATE.encode() not in response.content
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    captured = capfd.readouterr()
    assert GOOD_CODE not in captured.err
    assert GOOD_CODE not in captured.out


# The code exchange and the checks.


def test_exchange_code_sends_pkce_and_no_secret(tmp_path: Path) -> None:
    stub = ZendeskStub()
    store = make_store(tmp_path / "cfg", stub)

    token_set = exchange_code(
        store, GOOD_CODE, "verifier-123", "read", "http://127.0.0.1:8766/callback"
    )

    assert len(stub.token_requests) == 1
    request = stub.token_requests[0]
    assert form_fields(request) == {
        "grant_type": "authorization_code",
        "code": GOOD_CODE,
        "redirect_uri": "http://127.0.0.1:8766/callback",
        "code_verifier": "verifier-123",
        "scope": "read",
        "client_id": CLIENT_ID,
        "expires_in": "3600",
        "refresh_token_expires_in": "7776000",
    }
    assert "authorization" not in request.headers
    assert token_set.access_token == "access-login"


def test_exchange_code_invalid_grant_says_code_refused(tmp_path: Path) -> None:
    stub = ZendeskStub()
    stub.overrides["/oauth/tokens"] = (400, {"error": "invalid_grant"})
    store = make_store(tmp_path / "cfg", stub)

    with pytest.raises(LoginError, match="refused the sign-in code") as raised:
        exchange_code(store, GOOD_CODE, "verifier-123", "read", "http://127.0.0.1:8766/callback")

    message = str(raised.value)
    assert "120 seconds" in message
    assert "redirect URI" in message
    assert "PKCE" in message
    assert "expired or was revoked" not in message
    assert raised.value.__context__ is None


def test_exchange_code_invalid_scope_message(tmp_path: Path) -> None:
    stub = ZendeskStub()
    stub.overrides["/oauth/tokens"] = (400, {"error": "invalid_scope"})
    store = make_store(tmp_path / "cfg", stub)

    with pytest.raises(LoginError, match="does not allow the scope read write"):
        exchange_code(
            store, GOOD_CODE, "verifier-123", "read write", "http://127.0.0.1:8766/callback"
        )


def test_exchange_code_other_error_keeps_store_message(tmp_path: Path) -> None:
    stub = ZendeskStub()
    stub.overrides["/oauth/tokens"] = (500, {"error": "server_error"})
    store = make_store(tmp_path / "cfg", stub)

    with pytest.raises(LoginError, match=r"Zendesk token request failed \(500, server_error\)"):
        exchange_code(store, GOOD_CODE, "verifier-123", "read", "http://127.0.0.1:8766/callback")


def test_fetch_signed_in_user_rejects_anonymous() -> None:
    stub = ZendeskStub()
    stub.overrides[USERS_ME_PATH] = (200, {"user": {"id": None, "email": None}})

    with pytest.raises(LoginError, match="anonymous"):
        fetch_signed_in_user(make_client(stub), SUBDOMAIN, "access-login")


@pytest.mark.parametrize(
    "body",
    [
        {"user": {"id": 5, "email": ""}},
        {"user": {"id": 5, "email": 5}},
        {"user": {"id": True, "email": "ada@example.com"}},
        [],
        {"user": []},
    ],
    ids=["empty-email", "non-string-email", "bool-id", "list-body", "list-user"],
)
def test_fetch_signed_in_user_rejects_bad_user(body: object) -> None:
    stub = ZendeskStub()
    stub.overrides[USERS_ME_PATH] = (200, body)

    with pytest.raises(LoginError, match="anonymous"):
        fetch_signed_in_user(make_client(stub), SUBDOMAIN, "access-login")


@pytest.mark.parametrize("body", [[], {"token": []}], ids=["list-body", "list-token"])
def test_fetch_granted_scopes_rejects_bad_shape(body: object) -> None:
    stub = ZendeskStub()
    stub.overrides[TOKENS_CURRENT_PATH] = (200, body)

    with pytest.raises(LoginError, match="unexpected answer"):
        fetch_granted_scopes(make_client(stub), SUBDOMAIN, "access-login")


@pytest.mark.parametrize(
    "user",
    [{"id": 5, "email": "a@example.com"}, {"id": 5, "email": "a@example.com", "name": None}],
    ids=["missing-name", "null-name"],
)
def test_fetch_signed_in_user_missing_name_gives_empty(user: dict[str, object]) -> None:
    stub = ZendeskStub()
    stub.overrides[USERS_ME_PATH] = (200, {"user": user})

    assert fetch_signed_in_user(make_client(stub), SUBDOMAIN, "access-login") == (
        "",
        "a@example.com",
    )


def test_get_with_bearer_does_not_follow_redirects() -> None:
    requests: list[httpx.Request] = []

    def redirect_away(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "evil.example":
            return httpx.Response(200, json=USER_BODY)
        return httpx.Response(302, headers={"Location": "https://evil.example/steal"})

    # The client follows redirects by default; the request's own setting must win.
    client = httpx.Client(
        transport=httpx.MockTransport(redirect_away), trust_env=False, follow_redirects=True
    )

    with pytest.raises(LoginError, match="returned 302"):
        fetch_signed_in_user(client, SUBDOMAIN, "access-login")

    assert len(requests) == 1


@pytest.mark.parametrize(
    ("path", "answer", "message"),
    [
        (USERS_ME_PATH, (200, b"<html>not json</html>"), "not JSON"),
        (TOKENS_CURRENT_PATH, (200, {"token": {"scopes": "read"}}), "unexpected answer"),
    ],
    ids=["non-json-body", "wrong-tokens-current-shape"],
)
def test_fetch_checks_reject_malformed_answers(
    path: str, answer: tuple[int, object], message: str
) -> None:
    stub = ZendeskStub()
    stub.overrides[path] = answer
    check = fetch_signed_in_user if path == USERS_ME_PATH else fetch_granted_scopes

    with pytest.raises(LoginError, match=message):
        check(make_client(stub), SUBDOMAIN, "access-login")


def test_get_with_bearer_error_has_no_token() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = httpx.Client(transport=httpx.MockTransport(refuse), trust_env=False)

    with pytest.raises(LoginError, match="ConnectError") as raised:
        _get_with_bearer(client, f"https://example.zendesk.com{USERS_ME_PATH}", "access-login")

    assert "access-login" not in str(raised.value)
    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_format_report_warns_on_narrower_scope() -> None:
    settings = LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level="management")
    token_path = Path("/home/ada/.config/zendesk-mcp/tokens.json")

    narrower = format_report(
        settings, token_path, "Ada Agent", "ada@example.com", frozenset({"read"})
    )
    equal = format_report(
        settings, token_path, "Ada Agent", "ada@example.com", frozenset({"read", "write"})
    )

    assert narrower[:3] == [
        "Signed in to example.zendesk.com as Ada Agent <ada@example.com>.",
        "Saved scope: read (requested: read write; ACCESS_LEVEL=management).",
        f"Tokens saved to {token_path}.",
    ]
    warnings = [line for line in narrower if line.startswith("Warning:")]
    assert len(warnings) == 1
    assert warnings == [
        "Warning: The saved scope is narrower than requested; tools that need write will not work."
    ]
    assert "Saved scope: read write (requested: read write; ACCESS_LEVEL=management)." in equal
    assert [line for line in equal if line.startswith("Warning:")] == []


# The whole login.


@pytest.mark.parametrize(
    ("access_level", "expected_scope"), [("readonly", "read"), ("management", "read write")]
)
def test_run_login_saves_tokens_and_reports_user(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], access_level: str, expected_scope: str
) -> None:
    stub = ZendeskStub()

    exit_code, browser, token_dir = run_flow(tmp_path, stub, access_level)
    out = capsys.readouterr().out

    assert browser.url is not None
    assert exit_code == 0
    assert statuses(browser.outcomes) == [404, 200]
    assert len(stub.token_requests) == 1
    form = form_fields(stub.token_requests[0])
    params = authorize_params(browser.url)
    assert params["code_challenge"] == _s256_challenge(form["code_verifier"])
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == form["redirect_uri"]
    assert params["scope"] == form["scope"] == expected_scope
    assert form["code"] == GOOD_CODE
    for request in stub.get_requests:
        assert request.headers["authorization"] == "Bearer access-login"
    token_path = token_dir / "tokens.json"
    assert token_path.is_file()
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_dir.stat().st_mode) == 0o700
    loaded = make_store(token_dir, stub).load()
    assert loaded is not None
    assert loaded.scope == expected_scope
    assert "Ada Agent" in out
    assert "ada@example.com" in out
    assert f"Saved scope: {expected_scope} (" in out


def test_run_login_output_never_contains_secrets(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    stub = ZendeskStub()

    exit_code, _, _ = run_flow(tmp_path, stub, "management")
    captured = capfd.readouterr()

    # Check that there is output first, so the absence checks below cannot pass vacuously.
    assert exit_code == 0
    assert "Ada Agent" in captured.out
    assert len(stub.token_requests) == 1
    verifier = form_fields(stub.token_requests[0])["code_verifier"]
    for secret in (*SECRETS, verifier):
        assert secret not in captured.out
        assert secret not in captured.err


def test_run_login_resaves_verified_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = ZendeskStub(granted_scopes=["read"])
    stub.scope_in_token_answer = False

    exit_code, _, token_dir = run_flow(tmp_path, stub, "management")
    out = capsys.readouterr().out

    assert exit_code == 0
    loaded = make_store(token_dir, stub).load()
    assert loaded is not None
    assert loaded.scope == "read"
    assert "Saved scope: read (requested: read write" in out
    assert "Warning: The saved scope is narrower than requested; tools that need write" in out


def test_run_login_check_failure_keeps_saved_tokens(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = ZendeskStub()
    stub.overrides[USERS_ME_PATH] = (500, {"error": "boom"})

    exit_code, _, token_dir = run_flow(tmp_path, stub, "readonly")
    captured = capsys.readouterr()

    assert exit_code == 1
    assert (token_dir / "tokens.json").is_file()
    token_path = token_dir / "tokens.json"
    assert f"error: Tokens were saved to {token_path}, but checking them failed" in captured.err
    assert "500" in captured.err
    assert "Tokens were saved" not in captured.out


def test_run_login_wider_grant_saves_only_requested(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = ZendeskStub(granted_scopes=["read", "write"])
    stub.token_answer_scope = "read write"

    exit_code, _, token_dir = run_flow(tmp_path, stub, "readonly")
    out = capsys.readouterr().out

    assert exit_code == 0
    assert saved_scope(token_dir, stub) == "read"
    assert "Saved scope: read (requested: read; ACCESS_LEVEL=readonly)." in out
    assert "Warning:" not in out


@pytest.mark.parametrize(
    ("answer_scope", "granted", "expected_saves"),
    [
        ("read write", ["read"], ["read write", "read"]),
        ("read", ["read", "write"], ["read"]),
    ],
    ids=["current-narrower", "answer-narrower"],
)
def test_run_login_narrower_grant_saves_granted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    answer_scope: str,
    granted: list[str],
    expected_saves: list[str],
) -> None:
    saves = record_saves(monkeypatch)
    stub = ZendeskStub(granted_scopes=granted)
    stub.token_answer_scope = answer_scope

    exit_code, _, token_dir = run_flow(tmp_path, stub, "management")
    out = capsys.readouterr().out

    assert exit_code == 0
    assert saves == expected_saves
    assert saved_scope(token_dir, stub) == "read"
    assert "Saved scope: read (requested: read write; ACCESS_LEVEL=management)." in out
    assert "Warning: The saved scope is narrower than requested; tools that need write" in out


@pytest.mark.parametrize(
    ("answer_scope", "granted"),
    [("read write", ["read", "write"]), ("read", ["read"])],
    ids=["equal", "narrower-answer-equal-current"],
)
def test_run_login_equal_grant_does_not_resave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answer_scope: str, granted: list[str]
) -> None:
    saves = record_saves(monkeypatch)
    stub = ZendeskStub(granted_scopes=granted)
    stub.token_answer_scope = answer_scope

    exit_code, _, _ = run_flow(tmp_path, stub, "management")

    assert exit_code == 0
    assert saves == [answer_scope]


def test_run_login_check_failure_saves_clipped_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stub = ZendeskStub()
    stub.token_answer_scope = "read write"
    stub.overrides[USERS_ME_PATH] = (500, {"error": "boom"})

    exit_code, _, token_dir = run_flow(tmp_path, stub, "readonly")
    capsys.readouterr()

    assert exit_code == 1
    assert saved_scope(token_dir, stub) == "read"


@pytest.mark.parametrize(
    ("access_level", "granted", "expected_scope"),
    [("readonly", [], "read"), ("management", ["read"], "read write")],
    ids=["readonly", "management-guard"],
)
def test_run_login_failed_resave_leaves_clipped_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    access_level: str,
    granted: list[str],
    expected_scope: str,
) -> None:
    saves = record_saves(monkeypatch, fail_on_call=2)
    stub = ZendeskStub(granted_scopes=granted)
    stub.token_answer_scope = "read write"
    browser = FakeBrowser()
    token_dir = tmp_path / "cfg"

    with pytest.raises(OSError, match="disk full"):
        run_login(
            LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level=access_level),
            http_client=make_client(stub),
            token_dir=token_dir,
            open_browser=browser,
            port=0,
            timeout_seconds=10,
        )
    browser.join()
    capsys.readouterr()

    assert len(saves) == 2
    assert saved_scope(token_dir, stub) == expected_scope


@pytest.mark.parametrize(
    "browser_kwargs",
    [{"opened": False}, {"raises": webbrowser.Error("no runnable browser")}],
    ids=["returns-false", "raises-webbrowser-error"],
)
def test_run_login_browser_unavailable_prints_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], browser_kwargs: dict[str, object]
) -> None:
    browser = FakeBrowser(**browser_kwargs)  # type: ignore[arg-type]

    exit_code, _, _ = run_flow(tmp_path, ZendeskStub(), "readonly", browser=browser)
    out = capsys.readouterr().out

    assert browser.url is not None
    assert exit_code == 0
    assert browser.url in out
    assert "Could not open a browser; open the URL above." in out


def test_run_login_browser_opened_prints_no_fallback_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    browser = FakeBrowser(opened=True)

    exit_code, _, _ = run_flow(tmp_path, ZendeskStub(), "readonly", browser=browser)
    out = capsys.readouterr().out

    assert browser.url is not None
    assert exit_code == 0
    assert browser.url in out
    assert "Could not open a browser" not in out


def test_run_login_no_browser_prints_neutral_line(tmp_path: Path) -> None:
    out = CallbackOnUrl()

    outcome = outcome_of(
        lambda: run_login(
            LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level="readonly"),
            http_client=make_client(ZendeskStub()),
            token_dir=tmp_path / "cfg",
            open_browser=None,
            port=0,
            timeout_seconds=10,
            out=out,
        )
    )
    if out.thread is not None:
        out.thread.join(5)
    text = out.getvalue()

    assert outcome == 0
    assert "Open the URL above in your browser." in text
    assert "Could not open a browser" not in text


def test_run_login_closes_port_when_browser_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bound: list[stdio_login._CallbackServer] = []
    real_bind = stdio_login.bind_callback_server

    def recording_bind(**kwargs: object) -> stdio_login._CallbackServer:
        server = real_bind(**kwargs)  # type: ignore[arg-type]
        bound.append(server)
        return server

    def crashing_browser(url: str) -> bool:
        raise RuntimeError("browser crashed")

    monkeypatch.setattr(stdio_login, "bind_callback_server", recording_bind)

    with pytest.raises(RuntimeError, match="browser crashed"):
        run_login(
            LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level="readonly"),
            http_client=make_client(ZendeskStub()),
            token_dir=tmp_path / "cfg",
            open_browser=crashing_browser,
            port=0,
            timeout_seconds=5,
        )

    # The server is still referenced here, so only an explicit close can have freed the port.
    assert len(bound) == 1
    assert bound[0].socket.fileno() == -1
    rebound = real_bind(port=bound[0].server_address[1], expected_state=STATE)
    rebound.server_close()


# main().


@pytest.fixture
def login_env(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Set the login env vars and patch `load_dotenv`; return the paths it was called with."""
    dotenv_calls: list[object] = []

    def fake_load_dotenv(path: object = None, **kwargs: object) -> bool:
        dotenv_calls.append(path)
        return False

    monkeypatch.setattr(stdio_login, "load_dotenv", fake_load_dotenv)
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", SUBDOMAIN)
    monkeypatch.setenv("ACCESS_LEVEL", "readonly")
    monkeypatch.setenv("ZENDESK_STDIO_OAUTH_CLIENT_ID", CLIENT_ID)
    return dotenv_calls


@pytest.mark.parametrize(
    ("case", "expected_code", "expected_err"),
    [
        ("missing-client-id", 2, "error: ZENDESK_STDIO_OAUTH_CLIENT_ID"),
        ("login-error", 1, "error: login failed"),
        ("token-store-error", 1, "error: needs macOS or Linux"),
        ("os-error", 1, "error: disk full"),
        ("keyboard-interrupt", 130, "Cancelled."),
    ],
)
def test_main_exit_codes(
    login_env: list[object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_code: int,
    expected_err: str,
) -> None:
    raised_by_case: dict[str, BaseException] = {
        "login-error": LoginError("login failed"),
        "token-store-error": TokenStoreError("needs macOS or Linux"),
        "os-error": OSError("disk full"),
        "keyboard-interrupt": KeyboardInterrupt(),
    }

    def fake_run_login(settings: LoginSettings, **kwargs: object) -> int:
        if case == "missing-client-id":
            pytest.fail("run_login must not run without settings")
        raise raised_by_case[case]

    if case == "missing-client-id":
        monkeypatch.delenv("ZENDESK_STDIO_OAUTH_CLIENT_ID")
    monkeypatch.setattr(stdio_login, "run_login", fake_run_login)

    exit_code = main([])

    assert exit_code == expected_code
    assert expected_err in capsys.readouterr().err


def test_main_no_browser_passes_no_opener(
    login_env: list[object], monkeypatch: pytest.MonkeyPatch
) -> None:
    openers: list[object] = []
    seen_settings: list[LoginSettings] = []

    def fake_run_login(settings: LoginSettings, **kwargs: object) -> int:
        seen_settings.append(settings)
        openers.append(kwargs["open_browser"])
        return 0

    def fail_open(url: str, *args: object, **kwargs: object) -> bool:
        pytest.fail("webbrowser.open must not be called with --no-browser")

    monkeypatch.setattr(stdio_login, "run_login", fake_run_login)
    monkeypatch.setattr(webbrowser, "open", fail_open)

    exit_code = main(["--no-browser"])

    assert exit_code == 0
    assert openers == [None]
    assert seen_settings == [
        LoginSettings(subdomain=SUBDOMAIN, client_id=CLIENT_ID, access_level="readonly")
    ]
    assert login_env == [REPO_ROOT / ".env"]


def test_main_default_opener_is_webbrowser_open(monkeypatch: pytest.MonkeyPatch) -> None:
    openers: list[object] = []

    def fake_login_from_env(open_browser: object) -> int:
        openers.append(open_browser)
        return 0

    monkeypatch.setattr(stdio_login, "_login_from_env", fake_login_from_env)

    assert main([]) == 0
    assert len(openers) == 1
    assert openers[0] is webbrowser.open


# Structure: login never runs inside the stdio server.


def test_server_and_stdio_oauth_never_import_stdio_login() -> None:
    for name in ("server.py", "stdio_oauth.py"):
        assert "stdio_login" not in imported_modules(REPO_ROOT / name), name


def test_stdio_login_never_imports_server_or_mcp() -> None:
    path = REPO_ROOT / "stdio_login.py"
    names = imported_modules(path)

    assert "stdio_oauth" in names
    assert "server" not in names
    for name in names:
        assert name != "mcp" and not name.startswith("mcp."), name
    # It runs only by hand: `main()` is called from the `__main__` guard and nowhere else.
    guards = []
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'":
            guards.append(node)
    assert len(guards) == 1
    assert "main()" in ast.unparse(guards[0])
