"""Offline tests for the stdio OAuth token store in `stdio_oauth.py`.

HTTP is stubbed: most tests use an `httpx.MockTransport`, and the two-process test a local
`http.server` on 127.0.0.1. Time comes from an injected clock. Every store lives under `tmp_path`,
so no test touches the real `~/.config`.
"""

import contextlib
import fcntl
import json
import logging
import os
import select
import stat
import subprocess
import sys
import threading
import time
import types
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
import pytest

import stdio_oauth
from stdio_oauth import (
    ACCESS_TOKEN_LIFETIME_SECONDS,
    LOGIN_HINT,
    REFRESH_SKEW_SECONDS,
    REFRESH_TOKEN_LIFETIME_SECONDS,
    TOKEN_REQUEST_TIMEOUT_SECONDS,
    LoginRequiredError,
    TokenRefreshError,
    TokenSet,
    TokenStore,
    TokenStoreError,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SUBDOMAIN = "example"
CLIENT_ID = "zendesk_mcp_stdio"
START = 1_763_072_400
# What the stub endpoint answers, on purpose different from what the store requests.
ANSWERED_EXPIRES_IN = 1800
ANSWERED_REFRESH_EXPIRES_IN = 2_592_000
SCHEMA_KEYS = {
    "version",
    "subdomain",
    "client_id",
    "access_token",
    "refresh_token",
    "scope",
    "access_token_expires_at",
    "refresh_token_expires_at",
    "obtained_at",
}

Handler = Callable[[httpx.Request], httpx.Response]


class FakeClock:
    """A wall clock that only moves when a test sets `now`."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TokenEndpoint:
    """Stub `/oauth/tokens` that rotates tokens like Zendesk.

    It accepts only the newest refresh token (`refresh-<generation>`). An accepted refresh, or any
    other grant, issues the next generation: `access-2`/`refresh-2`, then `access-3`/... A refresh
    token it does not accept gets `400 {"error": "invalid_grant"}`. Call it as a `MockTransport`
    handler, or call `respond()` from a real HTTP server.
    """

    def __init__(
        self, *, generation: int = 1, delay_seconds: float = 0.0, minimal_response: bool = False
    ) -> None:
        self.generation = generation
        self.delay_seconds = delay_seconds
        self.minimal_response = minimal_response
        self.requests: list[httpx.Request] = []
        self.refresh_count = 0
        self.invalid_grants = 0
        self._lock = threading.Lock()

    def respond(self, fields: dict[str, str]) -> tuple[int, dict[str, object]]:
        with self._lock:
            status, body = self._answer(fields)
        time.sleep(self.delay_seconds)
        return status, body

    def _answer(self, fields: dict[str, str]) -> tuple[int, dict[str, object]]:
        if fields.get("grant_type") == "refresh_token":
            if fields.get("refresh_token") != f"refresh-{self.generation}":
                self.invalid_grants += 1
                return 400, {
                    "error": "invalid_grant",
                    "error_description": "The provided access grant is invalid, expired, or revoked.",
                }
            self.refresh_count += 1
        self.generation += 1
        body: dict[str, object] = {
            "access_token": f"access-{self.generation}",
            "refresh_token": f"refresh-{self.generation}",
            "token_type": "bearer",
            "expires_in": ANSWERED_EXPIRES_IN,
        }
        if not self.minimal_response:
            body["scope"] = "read write"
            body["refresh_token_expires_in"] = ANSWERED_REFRESH_EXPIRES_IN
        return 201, body

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        status, body = self.respond(form_fields(request))
        return httpx.Response(status, json=body)


def form_fields(request: httpx.Request) -> dict[str, str]:
    """Return the form-encoded body of a recorded request as a plain dict."""
    return dict(parse_qsl(request.content.decode()))


def make_token_set(**overrides: object) -> TokenSet:
    """Return a valid generation-1 token set; keyword arguments replace fields."""
    values: dict[str, object] = {
        "subdomain": SUBDOMAIN,
        "client_id": CLIENT_ID,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "scope": "read write",
        "access_token_expires_at": START + 3600,
        "refresh_token_expires_at": START + 7_776_000,
        "obtained_at": START,
    }
    values.update(overrides)
    return TokenSet(**values)  # type: ignore[arg-type]


def write_token_file(directory: Path, **overrides: object) -> Path:
    """Write a token file the way the store would (0700 directory, 0600 file), without the store."""
    data: dict[str, object] = {
        "version": 1,
        "subdomain": SUBDOMAIN,
        "client_id": CLIENT_ID,
        "access_token": "access-1",
        "refresh_token": "refresh-1",
        "scope": "read write",
        "access_token_expires_at": START + 3600,
        "refresh_token_expires_at": START + 7_776_000,
        "obtained_at": START,
    }
    data.update(overrides)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / "tokens.json"
    path.write_text(json.dumps(data))
    path.chmod(0o600)
    return path


def make_store(
    directory: Path, handler: Handler, clock: FakeClock | None = None, **kwargs: object
) -> TokenStore:
    """Build a store on `directory` whose HTTP goes to `handler`."""
    return TokenStore(
        subdomain=SUBDOMAIN,
        client_id=CLIENT_ID,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        directory=directory,
        clock=clock or FakeClock(),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.fixture
def token_dir(tmp_path: Path) -> Path:
    return tmp_path / "zendesk-mcp"


def test_token_set_repr_hides_tokens(token_dir: Path) -> None:
    token_set = make_token_set(access_token="secret-access", refresh_token="secret-refresh")
    store = make_store(token_dir, TokenEndpoint())

    texts = [repr(token_set), str(token_set), repr(store), str(store)]

    for text in texts:
        assert "secret-access" not in text
        assert "secret-refresh" not in text
    assert repr(store) == f"TokenStore(path={token_dir / 'tokens.json'})"


def test_save_creates_0600_file_in_0700_dir(token_dir: Path) -> None:
    token_dir.mkdir(mode=0o755)
    os.chmod(token_dir, 0o755)
    store = make_store(token_dir, TokenEndpoint())
    token_set = make_token_set()

    store.save(token_set)

    assert store.path.exists()
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(token_dir).st_mode) == 0o700
    data = json.loads(store.path.read_text())
    assert set(data) == SCHEMA_KEYS
    assert data["version"] == 1
    assert TokenSet.from_json(data) == token_set
    assert store.load() == token_set
    assert token_set.scopes == frozenset({"read", "write"})


def test_save_returns_token_path(token_dir: Path) -> None:
    store = make_store(token_dir, TokenEndpoint())

    saved_path = store.save(make_token_set())

    assert saved_path == token_dir / "tokens.json"
    assert saved_path == store.path
    assert saved_path.exists()


def test_save_failure_keeps_old_file_and_no_temp(
    token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write_token_file(token_dir)
    before = path.read_bytes()
    store = make_store(token_dir, TokenEndpoint())

    def failing_replace(source: object, destination: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(stdio_oauth.os, "replace", failing_replace)

    with pytest.raises(OSError, match="disk full"):
        store.save(make_token_set(access_token="access-9", refresh_token="refresh-9"))

    assert path.read_bytes() == before
    assert list(token_dir.glob(".tokens.*.tmp")) == []


def test_load_returns_none_without_file(token_dir: Path) -> None:
    endpoint = TokenEndpoint()
    store = make_store(token_dir, endpoint)

    assert store.load() is None
    with pytest.raises(LoginRequiredError, match=LOGIN_HINT):
        store.get_access_token()
    with pytest.raises(LoginRequiredError, match=LOGIN_HINT):
        store.refresh_access_token("access-1")
    assert not token_dir.exists()
    assert endpoint.requests == []


@pytest.mark.parametrize(
    "overrides",
    [{"subdomain": "other"}, {"client_id": "other_client"}],
    ids=["other-subdomain", "other-client"],
)
def test_load_rejects_other_subdomain_or_client(token_dir: Path, overrides: dict[str, str]) -> None:
    write_token_file(token_dir, **overrides)
    store = make_store(token_dir, TokenEndpoint())

    with pytest.raises(LoginRequiredError, match=LOGIN_HINT):
        store.load()


@pytest.mark.parametrize(
    "mode", [0o640, 0o604, 0o620], ids=["group-read", "other-read", "group-write"]
)
def test_load_rejects_group_readable_file(token_dir: Path, mode: int) -> None:
    path = write_token_file(token_dir)
    path.chmod(mode)
    store = make_store(token_dir, TokenEndpoint())

    with pytest.raises(TokenStoreError) as raised:
        store.load()

    assert not isinstance(raised.value, LoginRequiredError)
    assert "chmod 600" in str(raised.value)
    assert str(path) in str(raised.value)
    assert "access-1" not in str(raised.value)


def test_get_access_token_returns_fresh_token_without_request(token_dir: Path) -> None:
    write_token_file(token_dir)
    endpoint = TokenEndpoint()
    store = make_store(token_dir, endpoint)

    assert store.get_access_token() == "access-1"
    assert endpoint.requests == []


def test_get_access_token_refreshes_within_skew(token_dir: Path) -> None:
    write_token_file(token_dir)
    endpoint = TokenEndpoint()
    clock = FakeClock(START + 3600 - REFRESH_SKEW_SECONDS + 1)
    store = make_store(token_dir, endpoint, clock)

    assert store.get_access_token() == "access-2"

    assert len(endpoint.requests) == 1
    saved = store.load()
    assert saved is not None
    assert saved.access_token == "access-2"
    # The rotated refresh token is the only one Zendesk still accepts.
    assert saved.refresh_token == "refresh-2"
    assert saved.access_token_expires_at == int(clock.now) + ANSWERED_EXPIRES_IN
    assert saved.refresh_token_expires_at == int(clock.now) + ANSWERED_REFRESH_EXPIRES_IN
    assert saved.obtained_at == int(clock.now)


def test_refresh_sends_lifetimes_and_no_client_secret(token_dir: Path) -> None:
    write_token_file(token_dir, access_token_expires_at=START + 60)
    endpoint = TokenEndpoint()
    client = httpx.Client(
        base_url="https://example.zendesk.com/api/v2",
        auth=("module@example.com/token", "module-key"),
        transport=httpx.MockTransport(endpoint),
    )
    store = TokenStore(
        subdomain=SUBDOMAIN,
        client_id=CLIENT_ID,
        http_client=client,
        directory=token_dir,
        clock=FakeClock(),
    )

    assert store.get_access_token() == "access-2"

    assert len(endpoint.requests) == 1
    request = endpoint.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://example.zendesk.com/oauth/tokens"
    assert form_fields(request) == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": CLIENT_ID,
        "scope": "read write",
        "expires_in": str(ACCESS_TOKEN_LIFETIME_SECONDS),
        "refresh_token_expires_in": str(REFRESH_TOKEN_LIFETIME_SECONDS),
    }
    assert form_fields(request)["expires_in"] == "3600"
    assert form_fields(request)["refresh_token_expires_in"] == "7776000"
    assert "Authorization" not in request.headers
    assert request.extensions["timeout"] == httpx.Timeout(TOKEN_REQUEST_TIMEOUT_SECONDS).as_dict()


def test_refresh_access_token_uses_token_rotated_by_other_process(token_dir: Path) -> None:
    write_token_file(token_dir, access_token="access-2", refresh_token="refresh-2")
    endpoint = TokenEndpoint(generation=2)
    store = make_store(token_dir, endpoint)

    assert store.refresh_access_token("access-1") == "access-2"
    assert endpoint.requests == []


def test_refresh_access_token_refreshes_when_rejected_token_is_current(token_dir: Path) -> None:
    write_token_file(token_dir)
    endpoint = TokenEndpoint()
    store = make_store(token_dir, endpoint)

    assert store.refresh_access_token("access-1") == "access-2"

    assert len(endpoint.requests) == 1
    saved = store.load()
    assert saved is not None
    assert saved.access_token == "access-2"


def test_get_access_token_skips_refresh_when_file_already_fresh(
    token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token_file(token_dir, access_token_expires_at=START + 60)
    endpoint = TokenEndpoint(generation=2)
    store_a = make_store(token_dir, endpoint)
    store_b = make_store(token_dir, endpoint)
    real_read = store_b._read
    stale_reads: list[TokenSet | None] = []

    def read_then_rotate_elsewhere() -> TokenSet | None:
        if stale_reads:
            return real_read()
        stale = real_read()
        stale_reads.append(stale)
        # Another process rotates between store B's unlocked read and its lock.
        store_a.save(make_token_set(access_token="access-2", refresh_token="refresh-2"))
        return stale

    monkeypatch.setattr(store_b, "_read", read_then_rotate_elsewhere)

    assert store_b.get_access_token() == "access-2"
    assert len(stale_reads) == 1
    assert endpoint.requests == []


def test_refresh_invalid_grant_raises_login_required(token_dir: Path) -> None:
    path = write_token_file(token_dir, access_token_expires_at=START + 60)
    before = path.read_bytes()
    endpoint = TokenEndpoint(generation=5)  # refresh-1 was revoked
    store = make_store(token_dir, endpoint)

    with pytest.raises(LoginRequiredError) as raised:
        store.get_access_token()

    assert LOGIN_HINT in str(raised.value)
    assert "access-1" not in str(raised.value)
    assert "refresh-1" not in str(raised.value)
    assert len(endpoint.requests) == 1
    assert path.read_bytes() == before


def test_expired_refresh_token_raises_login_required_without_request(token_dir: Path) -> None:
    write_token_file(
        token_dir, access_token_expires_at=START - 10, refresh_token_expires_at=START - 1
    )
    endpoint = TokenEndpoint()
    store = make_store(token_dir, endpoint)

    with pytest.raises(LoginRequiredError, match=LOGIN_HINT):
        store.get_access_token()
    assert endpoint.requests == []


def _respond(status: int, body: object) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


def _respond_text(status: int, text: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return handler


def _connect_timeout(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectTimeout("timed out", request=request)


@pytest.mark.parametrize(
    ("handler", "expected_message"),
    [
        (_respond(500, {}), "Zendesk token request failed (500, unknown)"),
        (_respond(429, {}), "Zendesk token request failed (429, unknown)"),
        (_connect_timeout, "Zendesk token request failed (ConnectTimeout, unknown)"),
        (
            _respond(200, {"access_token": "access-2"}),
            "Zendesk token request failed (malformed response, unknown)",
        ),
        (
            _respond(201, ["access-2", "refresh-2"]),
            "Zendesk token request failed (malformed response, unknown)",
        ),
        (_respond(400, {"error": 123}), "Zendesk token request failed (400, unknown)"),
        (
            _respond(400, {"error": "invalid_grant refresh-1"}),
            "Zendesk token request failed (400, unknown)",
        ),
        (
            _respond(400, {"error": "invalid_request"}),
            "Zendesk token request failed (400, invalid_request)",
        ),
    ],
    ids=[
        "500",
        "429",
        "connect-timeout",
        "malformed-200",
        "body-not-object",
        "error-not-str",
        "error-not-exact",
        "other-oauth-error",
    ],
)
def test_refresh_server_error_raises_refresh_error_and_keeps_file(
    token_dir: Path, handler: Handler, expected_message: str
) -> None:
    path = write_token_file(token_dir, access_token_expires_at=START + 60)
    before = path.read_bytes()
    store = make_store(token_dir, handler)

    with pytest.raises(TokenRefreshError) as raised:
        store.get_access_token()

    assert not isinstance(raised.value, LoginRequiredError)
    assert str(raised.value) == expected_message
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("handler", "file_text", "expected_error"),
    [
        (
            _respond(400, {"error": "invalid_grant refresh-1", "error_description": "refresh-1"}),
            None,
            TokenRefreshError,
        ),
        (
            _respond(400, {"error": "invalid_grant", "error_description": "refresh-1 access-1"}),
            None,
            LoginRequiredError,
        ),
        (_connect_timeout, None, TokenRefreshError),
        (_respond_text(500, "<html>refresh-1 access-1</html>"), None, TokenRefreshError),
        (
            TokenEndpoint(),
            '{"access_token": "access-1", "refresh_token": "refresh-1"',
            TokenStoreError,
        ),
        (
            TokenEndpoint(),
            json.dumps({"version": 1, "access_token": "access-1", "refresh_token": ["refresh-1"]}),
            TokenStoreError,
        ),
        (TokenEndpoint(), json.dumps(["access-1", "refresh-1"]), TokenStoreError),
    ],
    ids=[
        "error-embeds-token",
        "invalid-grant",
        "connect-timeout",
        "html-500",
        "corrupt-file",
        "mistyped-file",
        "file-not-object",
    ],
)
def test_errors_and_logs_never_contain_tokens(
    token_dir: Path,
    caplog: pytest.LogCaptureFixture,
    handler: Handler,
    file_text: str | None,
    expected_error: type[TokenStoreError],
) -> None:
    caplog.set_level(logging.DEBUG)
    path = write_token_file(token_dir, access_token_expires_at=START + 60)
    if file_text is not None:
        path.write_text(file_text)
    store = make_store(token_dir, handler)

    with pytest.raises(expected_error) as raised:
        store.get_access_token()

    error = raised.value
    assert error.__cause__ is None
    assert error.__context__ is None
    for secret in ("access-1", "refresh-1"):
        assert secret not in str(error)
        assert secret not in repr(error)
        assert secret not in caplog.text


def test_two_stores_in_threads_refresh_once(token_dir: Path) -> None:
    write_token_file(token_dir, access_token_expires_at=START + 60)
    endpoint = TokenEndpoint(delay_seconds=0.2)
    stores = [make_store(token_dir, endpoint), make_store(token_dir, endpoint)]
    thread_count = 8
    barrier = threading.Barrier(thread_count, timeout=10)
    results: list[str] = []
    errors: list[TokenStoreError] = []
    results_lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            barrier.wait()
            token = stores[index % 2].get_access_token()
        except TokenStoreError as error:  # collected and asserted on below
            with results_lock:
                errors.append(error)
            return
        with results_lock:
            results.append(token)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(thread_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert results == ["access-2"] * thread_count
    assert len(endpoint.requests) == 1
    assert endpoint.invalid_grants == 0


CHILD_SCRIPT = """
import sys
from pathlib import Path

import httpx

import stdio_oauth

store = stdio_oauth.TokenStore(
    subdomain=sys.argv[3],
    client_id=sys.argv[4],
    http_client=httpx.Client(trust_env=False),
    directory=Path(sys.argv[1]),
    token_url=sys.argv[2],
)
print("ready", flush=True)
if sys.stdin.readline().strip() != "go":
    sys.exit(2)
print(store.get_access_token(), flush=True)
"""


@pytest.fixture
def token_server() -> Iterator[tuple[str, TokenEndpoint]]:
    """Serve a rotating `/oauth/tokens` on 127.0.0.1; shut it down after the test."""
    endpoint = TokenEndpoint(delay_seconds=0.3)

    class RequestHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/oauth/tokens":
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length", "0"))
            fields = dict(parse_qsl(self.rfile.read(length).decode()))
            status, body = endpoint.respond(fields)
            payload = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            return  # keep the test output quiet

    server = ThreadingHTTPServer(("127.0.0.1", 0), RequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/oauth/tokens", endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _read_line(child: subprocess.Popen[str], timeout_seconds: float) -> str:
    """Read one stdout line from `child`, or return "" if none arrives in time."""
    readable, _, _ = select.select([child.stdout], [], [], timeout_seconds)
    if not readable or child.stdout is None:
        return ""
    return child.stdout.readline().strip()


def test_two_processes_refresh_once(
    token_dir: Path, token_server: tuple[str, TokenEndpoint]
) -> None:
    token_url, endpoint = token_server
    now = int(time.time())
    write_token_file(
        token_dir, access_token_expires_at=now - 1, refresh_token_expires_at=now + 86_400
    )
    command = [sys.executable, "-c", CHILD_SCRIPT, str(token_dir), token_url, SUBDOMAIN, CLIENT_ID]
    children: list[subprocess.Popen[str]] = []
    try:
        for _ in range(2):
            children.append(
                subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=REPO_ROOT,
                    env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
                )
            )
        # Both children are built and waiting before either one starts; otherwise one could
        # finish before the other begins and the test would pass with no lock at all.
        for child in children:
            assert _read_line(child, timeout_seconds=20) == "ready"
        for child in children:
            assert child.stdin is not None
            child.stdin.write("go\n")
            child.stdin.flush()
        outputs = [child.communicate(timeout=20) for child in children]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=5)

    for child, (stdout, stderr) in zip(children, outputs, strict=True):
        assert child.returncode == 0, stderr
        assert stdout.strip() == "access-2"
    assert endpoint.refresh_count == 1
    assert endpoint.invalid_grants == 0


def _save_in_thread(store: TokenStore, timeout_seconds: float = 10) -> BaseException | None:
    """Call `store.save()` in a thread; return what it raised. Fails the test if it hangs."""
    raised: list[BaseException] = []

    def target() -> None:
        try:
            store.save(make_token_set())
        except TokenStoreError as error:
            raised.append(error)

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout_seconds)
    assert not thread.is_alive(), "save() did not return; a lock wait has no timeout"
    return raised[0] if raised else None


def test_lock_timeout_raises(token_dir: Path) -> None:
    token_dir.mkdir(mode=0o700)
    store = make_store(token_dir, TokenEndpoint(), lock_timeout_seconds=0.2)
    lock_fd = os.open(token_dir / "tokens.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        error = _save_in_thread(store)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)
    assert isinstance(error, TokenStoreError)
    assert "Timed out waiting for the token lock" in str(error)

    # The in-process thread lock has the same timeout.
    assert store._thread_lock.acquire(timeout=5)
    try:
        error = _save_in_thread(store)
    finally:
        store._thread_lock.release()
    assert isinstance(error, TokenStoreError)
    assert "Timed out waiting for the token lock" in str(error)
    assert not store.path.exists()


class _SlowThreadLock:
    """Stands in for the store's thread lock: acquiring it moves the fake monotonic clock on."""

    def __init__(self, clock: FakeClock, wait_seconds: float) -> None:
        self._clock = clock
        self._wait_seconds = wait_seconds

    def acquire(self, timeout: float = -1) -> bool:
        self._clock.now += self._wait_seconds
        return True

    def release(self) -> None:
        return None


def test_thread_lock_wait_counts_against_flock_deadline(
    token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monotonic = FakeClock(1000.0)
    fake_time = types.SimpleNamespace(monotonic=monotonic, sleep=time.sleep, time=time.time)
    monkeypatch.setattr(stdio_oauth, "time", fake_time)
    store = make_store(token_dir, TokenEndpoint(), lock_timeout_seconds=10.0)
    monkeypatch.setattr(store, "_thread_lock", _SlowThreadLock(monotonic, wait_seconds=7.0))
    real_wait_for_flock = stdio_oauth._wait_for_flock
    deadlines: list[float] = []

    def spy_wait_for_flock(lock_fd: int, deadline: float) -> bool:
        deadlines.append(deadline)
        return real_wait_for_flock(lock_fd, deadline)

    monkeypatch.setattr(stdio_oauth, "_wait_for_flock", spy_wait_for_flock)

    store.save(make_token_set())

    # One budget: 7 s spent on the thread lock leaves 3 s for the flock, not a fresh 10 s.
    assert deadlines == [1010.0]


def test_locks_are_released_after_a_failed_refresh(token_dir: Path) -> None:
    write_token_file(token_dir, access_token_expires_at=START + 60)
    endpoint = TokenEndpoint()
    answers = [_respond(500, {})]

    def fail_once_then_rotate(request: httpx.Request) -> httpx.Response:
        if answers:
            return answers.pop()(request)
        return endpoint(request)

    store = make_store(token_dir, fail_once_then_rotate, lock_timeout_seconds=0.5)

    with pytest.raises(TokenRefreshError):
        store.get_access_token()

    # Neither the thread lock nor the flock may still be held.
    assert store.get_access_token() == "access-2"
    lock_fd = os.open(token_dir / "tokens.lock", os.O_RDWR)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def test_redirect_is_not_followed(token_dir: Path) -> None:
    path = write_token_file(token_dir, access_token_expires_at=START + 60)
    before = path.read_bytes()
    endpoint = TokenEndpoint()
    requests: list[httpx.Request] = []

    def redirect_elsewhere(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "example.zendesk.com":
            return httpx.Response(
                307, headers={"Location": "https://elsewhere.example/oauth/tokens"}
            )
        return endpoint(request)

    store = TokenStore(
        subdomain=SUBDOMAIN,
        client_id=CLIENT_ID,
        http_client=httpx.Client(transport=httpx.MockTransport(redirect_elsewhere)),
        directory=token_dir,
        clock=FakeClock(),
    )

    with pytest.raises(TokenRefreshError) as raised:
        store.get_access_token()

    assert str(raised.value) == "Zendesk token request failed (307, unknown)"
    # The refresh token was sent once, to the token URL only.
    assert [request.url.host for request in requests] == ["example.zendesk.com"]
    assert path.read_bytes() == before


def test_invalid_grant_on_401_raises_login_required(token_dir: Path) -> None:
    path = write_token_file(token_dir, access_token_expires_at=START + 60)
    before = path.read_bytes()
    store = make_store(token_dir, _respond(401, {"error": "invalid_grant"}))

    with pytest.raises(LoginRequiredError, match=LOGIN_HINT):
        store.get_access_token()
    assert path.read_bytes() == before


def test_answer_scope_wins_over_fallback(token_dir: Path) -> None:
    parsed = parse_answer(
        answer_with("scope", "read"),
        previous=make_token_set(scope="read write"),
        scope="read write",
    )
    assert parsed.scope == "read"

    write_token_file(token_dir, scope="read write", access_token_expires_at=START + 60)
    store = make_store(token_dir, _respond(201, answer_with("scope", "read")))

    assert store.get_access_token() == "access-2"
    saved = store.load()
    assert saved is not None
    assert saved.scope == "read"


def test_store_requires_fcntl(token_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stdio_oauth, "fcntl", None)

    with pytest.raises(TokenStoreError, match="macOS or Linux"):
        make_store(token_dir, TokenEndpoint())


def test_refresh_without_scope_in_response_keeps_stored_scope(token_dir: Path) -> None:
    # No known refresh-token expiry: the store must still refresh.
    write_token_file(
        token_dir, scope="read", access_token_expires_at=START + 60, refresh_token_expires_at=None
    )
    endpoint = TokenEndpoint(minimal_response=True)
    store = make_store(token_dir, endpoint)

    assert store.get_access_token() == "access-2"
    saved = store.load()
    assert saved is not None
    assert saved.scope == "read"

    exchanged = store.request_tokens(
        {"grant_type": "authorization_code", "code": "code-1", "scope": "read"}, previous=None
    )

    assert exchanged.access_token == "access-3"
    assert exchanged.scope == "read"
    assert exchanged.refresh_token_expires_at is None
    # request_tokens does not save; the caller decides.
    reloaded = store.load()
    assert reloaded is not None
    assert reloaded.access_token == "access-2"


def test_get_access_token_refreshes_when_file_token_differs_but_is_expiring(
    token_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token_file(token_dir, access_token_expires_at=START + 60)
    endpoint = TokenEndpoint(generation=2)  # only refresh-2 is still accepted
    store_a = make_store(token_dir, endpoint)
    store_b = make_store(token_dir, endpoint)
    real_read = store_b._read
    stale_reads: list[TokenSet | None] = []

    def read_then_rotate_elsewhere() -> TokenSet | None:
        if stale_reads:
            return real_read()
        stale = real_read()
        stale_reads.append(stale)
        # Another process rotated, but its new token is already inside the skew window too.
        store_a.save(
            make_token_set(
                access_token="access-2",
                refresh_token="refresh-2",
                access_token_expires_at=START + 60,
            )
        )
        return stale

    monkeypatch.setattr(store_b, "_read", read_then_rotate_elsewhere)

    assert store_b.get_access_token() == "access-3"
    assert len(stale_reads) == 1
    assert len(endpoint.requests) == 1
    assert form_fields(endpoint.requests[0])["refresh_token"] == "refresh-2"
    saved = store_b.load()
    assert saved is not None
    assert saved.access_token == "access-3"


VALID_ANSWER: dict[str, object] = {
    "access_token": "access-2",
    "refresh_token": "refresh-2",
    "token_type": "bearer",
    "scope": "read write",
    "expires_in": ANSWERED_EXPIRES_IN,
    "refresh_token_expires_in": ANSWERED_REFRESH_EXPIRES_IN,
}
MISSING = object()


def answer_with(field_name: str, value: object) -> dict[str, object]:
    """Return a valid token answer with one field replaced, or removed when `value` is MISSING."""
    body = dict(VALID_ANSWER)
    if value is MISSING:
        del body[field_name]
    else:
        body[field_name] = value
    return body


def parse_answer(body: object, previous: TokenSet | None = None, scope: str = "") -> TokenSet:
    """Run `TokenSet.from_token_response` with the test subdomain, client and START clock."""
    return TokenSet.from_token_response(
        body,
        subdomain=SUBDOMAIN,
        client_id=CLIENT_ID,
        now=START + 0.5,
        previous=previous,
        requested_scope=scope,
    )


def parse_kept_answer(body: object) -> TokenSet:
    """Parse an answer that must be kept: Zendesk has already rotated when a 2xx arrives."""
    try:
        return parse_answer(body)
    except TokenRefreshError as error:
        pytest.fail(f"a 2xx answer with both tokens was rejected: {error}")


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("access_token", MISSING),
        ("access_token", 123),
        ("refresh_token", MISSING),
        ("refresh_token", 123),
    ],
    ids=["access-missing", "access-int", "refresh-missing", "refresh-int"],
)
def test_from_token_response_rejects_bad_field(field_name: str, value: object) -> None:
    with pytest.raises(TokenRefreshError, match="malformed response"):
        parse_answer(answer_with(field_name, value))


@pytest.mark.parametrize(
    "expires_in",
    [MISSING, None, "3600", 1800.0, True],
    ids=["missing", "null", "string", "float", "bool"],
)
def test_from_token_response_without_valid_expires_in_uses_requested_lifetime(
    token_dir: Path, expires_in: object
) -> None:
    body = answer_with("expires_in", expires_in)
    write_token_file(token_dir, access_token_expires_at=START + 60)
    store = make_store(token_dir, _respond(201, body))

    # Zendesk has already rotated, so the store must keep and save these tokens.
    try:
        returned = store.get_access_token()
    except TokenRefreshError as error:
        pytest.fail(f"a 2xx answer with both tokens was rejected: {error}")

    assert returned == "access-2"
    saved = store.load()
    assert saved is not None
    assert saved.refresh_token == "refresh-2"
    assert saved.access_token_expires_at == START + ACCESS_TOKEN_LIFETIME_SECONDS
    parsed = parse_kept_answer(body)
    assert parsed.access_token_expires_at == START + ACCESS_TOKEN_LIFETIME_SECONDS
    assert type(parsed.access_token_expires_at) is int


@pytest.mark.parametrize(
    "refresh_lifetime",
    ["7776000", 7_776_000.0, True, None],
    ids=["string", "float", "bool", "null"],
)
def test_from_token_response_invalid_refresh_lifetime_stores_null_expiry(
    refresh_lifetime: object,
) -> None:
    parsed = parse_kept_answer(answer_with("refresh_token_expires_in", refresh_lifetime))

    assert parsed.access_token == "access-2"
    assert parsed.refresh_token == "refresh-2"
    assert parsed.refresh_token_expires_at is None


@pytest.mark.parametrize("scope", [123, ["read"]], ids=["int", "list"])
def test_from_token_response_non_string_scope_falls_back(scope: object) -> None:
    parsed = parse_answer(answer_with("scope", scope), previous=make_token_set(scope="read write"))

    assert parsed.scope == "read write"


def test_from_token_response_prefers_previous_scope_over_requested() -> None:
    parsed = parse_answer(
        answer_with("scope", MISSING), previous=make_token_set(scope="read write"), scope="read"
    )

    assert parsed.scope == "read write"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("access_token_expires_at", "1763076000"),
        ("access_token_expires_at", True),
        ("obtained_at", "1763072400"),
        ("obtained_at", True),
        ("refresh_token_expires_at", "1770852000"),
        ("refresh_token_expires_at", True),
        ("subdomain", 123),
        ("subdomain", None),
        ("client_id", 123),
        ("client_id", None),
        ("access_token", 123),
        ("access_token", None),
        ("refresh_token", 123),
        ("refresh_token", None),
        ("scope", 123),
        ("scope", None),
    ],
    ids=lambda value: repr(value),
)
def test_load_rejects_bad_field_type(token_dir: Path, field_name: str, value: object) -> None:
    write_token_file(token_dir, **{field_name: value})
    store = make_store(token_dir, TokenEndpoint())

    with pytest.raises(TokenStoreError) as raised:
        store.load()

    assert not isinstance(raised.value, LoginRequiredError)
    assert "access-1" not in str(raised.value)
    assert "refresh-1" not in str(raised.value)


def test_refresh_omits_empty_scope(token_dir: Path) -> None:
    write_token_file(token_dir, scope="", access_token_expires_at=START + 60)
    endpoint = TokenEndpoint()
    store = make_store(token_dir, endpoint)

    assert store.get_access_token() == "access-2"

    assert len(endpoint.requests) == 1
    # The raw body: a form parser would drop an empty `scope=` and hide it.
    assert b"scope" not in endpoint.requests[0].content
