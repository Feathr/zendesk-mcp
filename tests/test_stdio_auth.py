"""Offline tests for stdio OAuth in `server.py`: bearer auth, refresh, write scope, fallback, guard.

Zendesk is stubbed by one `httpx.MockTransport` handler (`FakeZendesk`) that serves both
`/oauth/tokens` and the API. Time comes from a fake clock. Nothing reaches the network.

Rules for every test in this module:
- Each test uses its own `tmp_path` token dir, set by monkeypatching `_STDIO_TOKEN_STORE` and
  `_STDIO_TOKEN_DIR` (see `use_oauth`). A test must never let the unpatched store write to the
  shared test HOME; the `no_leftover_token_file` fixture in conftest fails a test that does.
- Patching `_client` alone does not reach the store's client. A test that can refresh builds its
  own store on the same handler, as `use_oauth` does.
"""

import importlib
import importlib.util
import itertools
import json
import os
import pwd
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from urllib.parse import parse_qsl

import anyio
import httpx
import pytest
from mcp.client import Client

import server
import stdio_oauth

SUBDOMAIN = "example"
CLIENT_ID = "zendesk_mcp_stdio"
START = 1_763_072_400
LOGIN_COMMAND = "stdio_login.py"
ENV_BASIC = ("env@example.com/token", "env-key")
ATTACHMENT_URL = "https://example.zendesk.com/attachments/token/abc/?name=a.txt"
ATTACHMENT_CHUNK = b"x" * 65536
ATTACHMENT_CHUNK_COUNT = 32  # 2 MiB in total, over a 1 MiB cap
SERVER_FILE = Path(__file__).resolve().parent.parent / "server.py"
_module_ids = itertools.count()


class FakeClock:
    """A wall clock that only moves when a test sets `now`."""

    def __init__(self, now: float = START) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def authorization(request: httpx.Request) -> str | None:
    """Return the request's Authorization header."""
    return request.headers.get("authorization")


def form_fields(request: httpx.Request) -> dict[str, str]:
    """Return the form-encoded body of a recorded token request."""
    return dict(parse_qsl(request.content.decode()))


def basic_header(credentials: tuple[str, str]) -> str:
    """Return the Authorization header value httpx sends for a basic-auth tuple."""
    request = httpx.Request("GET", "https://example.zendesk.com/")
    flow = httpx.BasicAuth(*credentials).auth_flow(request)
    return next(flow).headers["authorization"]


class FakeZendesk:
    """Stub Zendesk: `/oauth/tokens` rotates like Zendesk, the API answers by `api_status`.

    The token endpoint accepts only the newest refresh token (`refresh-<generation>`) and issues
    `access-<n+1>`/`refresh-<n+1>`. `refresh_mode` "invalid_grant" refuses every refresh, and
    "server_error" answers 500. The API answers 401 to a bearer token in `rejected_tokens`, or
    whatever `api_status` returns.
    """

    def __init__(self, *, refresh_mode: str = "rotate") -> None:
        self.generation = 1
        self.refresh_mode = refresh_mode
        self.rejected_tokens: set[str] = set()
        self.api_status: Callable[[httpx.Request], int] | None = None
        self.on_api: Callable[[httpx.Request], None] | None = None
        self.api_requests: list[httpx.Request] = []
        self.token_requests: list[httpx.Request] = []
        self.attachment_chunks_sent = 0
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/tokens":
            return self._token_answer(request)
        # A copy: the auth flow changes the same request's headers before it retries.
        snapshot = httpx.Request(
            request.method, request.url, headers=request.headers.copy(), content=request.content
        )
        with self._lock:
            self.api_requests.append(snapshot)
        if self.on_api is not None:
            self.on_api(request)
        status = self._status_for(request)
        if status != 200:
            return httpx.Response(status, json={"error": f"stub {status}"})
        return self._api_answer(request)

    def _status_for(self, request: httpx.Request) -> int:
        bearer = (authorization(request) or "").removeprefix("Bearer ")
        if bearer in self.rejected_tokens:
            return 401
        if self.api_status is not None:
            return self.api_status(request)
        return 200

    def _token_answer(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.token_requests.append(request)
            fields = form_fields(request)
            if self.refresh_mode == "server_error":
                return httpx.Response(500, json={"error": "server_error"})
            accepted = fields.get("refresh_token") == f"refresh-{self.generation}"
            if self.refresh_mode == "invalid_grant" or not accepted:
                return httpx.Response(400, json={"error": "invalid_grant"})
            self.generation += 1
            body = {
                "access_token": f"access-{self.generation}",
                "refresh_token": f"refresh-{self.generation}",
                "token_type": "bearer",
                "scope": fields.get("scope", "read write"),
                "expires_in": 3600,
                "refresh_token_expires_in": 7_776_000,
            }
        return httpx.Response(201, json=body)

    def _api_answer(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/attachments/"):
            return httpx.Response(
                200, headers={"content-type": "text/plain"}, content=self._attachment_body()
            )
        if path.endswith("/uploads.json"):
            return httpx.Response(201, json={"upload": {"token": "upload-token"}})
        if path.endswith("/comments.json"):
            return httpx.Response(200, json={"comments": [], "next_page": None})
        return httpx.Response(200, json={"ticket": {"id": 1, "status": "open", "tags": ["x"]}})

    def _attachment_body(self) -> Iterator[bytes]:
        for _ in range(ATTACHMENT_CHUNK_COUNT):
            self.attachment_chunks_sent += 1
            yield ATTACHMENT_CHUNK

    def api_authorizations(self) -> list[str | None]:
        """Return the Authorization header of every API request, in order."""
        return [authorization(request) for request in self.api_requests]


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


@dataclass
class OAuthSetup:
    """What `use_oauth` installed on a server module."""

    zendesk: FakeZendesk
    token_dir: Path
    store: stdio_oauth.TokenStore
    client: httpx.Client
    clock: FakeClock


def use_oauth(
    module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    zendesk: FakeZendesk,
    *,
    write_file: bool = True,
    with_store: bool = True,
    **token_overrides: object,
) -> OAuthSetup:
    """Point a server module at a stub Zendesk and a token store on `tmp_path`.

    Args:
        module: The real `server` module or a loaded copy.
        monkeypatch: The test's monkeypatch.
        tmp_path: The test's temp dir; the token dir is `tmp_path / "zendesk-mcp"`.
        zendesk: The stub that answers the API and the token endpoint.
        write_file: Write a generation-1 token file (with `token_overrides`).
        with_store: Install the store; False leaves `_STDIO_TOKEN_STORE` None (no client ID).
        **token_overrides: Fields that replace the token file's defaults.

    Returns:
        The installed setup.
    """
    token_dir = tmp_path / "zendesk-mcp"
    clock = FakeClock()
    client = httpx.Client(
        base_url=module.BASE_URL,
        headers={"Accept": "application/json"},
        transport=httpx.MockTransport(zendesk),
    )
    store = stdio_oauth.TokenStore(
        subdomain=SUBDOMAIN,
        client_id=CLIENT_ID,
        http_client=client,
        directory=token_dir,
        clock=clock,
    )
    if write_file:
        write_token_file(token_dir, **token_overrides)
    monkeypatch.setattr(module, "_client", client)
    monkeypatch.setattr(module, "_STDIO_TOKEN_STORE", store if with_store else None, raising=False)
    monkeypatch.setattr(module, "_STDIO_TOKEN_DIR", token_dir, raising=False)
    monkeypatch.setattr(module, "_fallback_warned", False, raising=False)
    return OAuthSetup(zendesk, token_dir, store, client, clock)


def load_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **env: str | None) -> ModuleType:
    """Import a fresh copy of server.py (no `.env` next to it) under the given env overrides.

    Every `MCP_*` var is cleared first; a `None` value deletes a var.
    """
    copy_dir = tmp_path / f"server_copy_{next(_module_ids)}"
    copy_dir.mkdir()
    shutil.copy(SERVER_FILE, copy_dir / "server.py")
    for name in list(os.environ):
        if name.startswith("MCP_"):
            monkeypatch.delenv(name)
    for name, value in env.items():
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    spec = importlib.util.spec_from_file_location(copy_dir.name, copy_dir / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_management(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """Import a server.py copy with ACCESS_LEVEL=management."""
    return load_copy(tmp_path, monkeypatch, ACCESS_LEVEL="management")


async def _call_tool_in_memory(module: ModuleType, name: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool through an in-memory MCP session and return (isError, first text content)."""
    async with Client(module.mcp) as session:
        result = await session.call_tool(name, arguments)
    return result.is_error, result.content[0].text


def call_tool(module: ModuleType, name: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool the way an MCP client does; tool errors come back as text."""
    return anyio.run(_call_tool_in_memory, module, name, arguments)


def warning_lines(stderr: str) -> list[str]:
    """Return the fallback warning lines in captured stderr."""
    return [line for line in stderr.splitlines() if "2027-04-30" in line]


class SlowWriter:
    """A stderr stand-in that takes 20 ms per write and keeps everything written."""

    def __init__(self) -> None:
        self._parts: list[str] = []

    def write(self, text: str) -> int:
        time.sleep(0.02)
        self._parts.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def text(self) -> str:
        """Return everything written so far."""
        return "".join(self._parts)


@pytest.fixture(autouse=True)
def reset_fallback_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let each test see the fallback warning once, whatever ran before it."""
    monkeypatch.setattr(server, "_fallback_warned", False, raising=False)


# ---------------------------------------------------------------------------
# Bearer auth, refresh and retry
# ---------------------------------------------------------------------------


def test_stdio_auth_sends_bearer_from_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a token file, a read sends the stored access token as a bearer token."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())

    server.get_ticket(1)

    assert setup.zendesk.api_authorizations() == ["Bearer access-1"]
    assert setup.zendesk.token_requests == []


@pytest.mark.parametrize(
    "call_helper",
    [
        lambda: server._get("/tickets/1.json"),
        lambda: server._get_with_retry("/tickets/1.json", deadline=None),
        lambda: server._post("/tickets.json", {"ticket": {}}),
        lambda: server._put("/tickets/1.json", {"ticket": {}}),
        lambda: server._delete("/tickets/1.json"),
        lambda: server._post_upload("a.txt", b"hello", "text/plain"),
    ],
    ids=["_get", "_get_with_retry", "_post", "_put", "_delete", "_post_upload"],
)
def test_stdio_auth_helpers_send_bearer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, call_helper: Callable[[], object]
) -> None:
    """Every HTTP helper sends the bearer token from the token file."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())

    call_helper()

    assert setup.zendesk.api_authorizations() == ["Bearer access-1"]


def test_stdio_auth_refreshes_within_skew(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An access token with 60 s left is refreshed first; the request carries the new token."""
    setup = use_oauth(
        server, monkeypatch, tmp_path, FakeZendesk(), access_token_expires_at=START + 60
    )

    server.get_ticket(1)

    assert len(setup.zendesk.token_requests) == 1
    assert form_fields(setup.zendesk.token_requests[0])["refresh_token"] == "refresh-1"
    assert setup.zendesk.api_authorizations() == ["Bearer access-2"]


def test_stdio_auth_retries_once_after_401(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 401 on the current token refreshes once and sends the request again with the new token."""
    zendesk = FakeZendesk()
    zendesk.rejected_tokens = {"access-1"}
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)

    result = json.loads(server.get_ticket(1))

    assert result["id"] == 1
    assert setup.zendesk.api_authorizations() == ["Bearer access-1", "Bearer access-2"]
    assert len(setup.zendesk.token_requests) == 1
    assert form_fields(setup.zendesk.token_requests[0])["refresh_token"] == "refresh-1"


def test_stdio_auth_second_401_is_not_retried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 after the retry is final: two API requests, one refresh, then an HTTP 401 error."""
    zendesk = FakeZendesk()
    zendesk.api_status = lambda request: 401
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        server.get_ticket(1)

    assert raised.value.response.status_code == 401
    assert setup.zendesk.api_authorizations() == ["Bearer access-1", "Bearer access-2"]
    assert len(setup.zendesk.token_requests) == 1


def test_stdio_auth_401_uses_token_rotated_by_other_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When another session rotated the file meanwhile, the retry uses its token with no POST."""
    zendesk = FakeZendesk()
    zendesk.rejected_tokens = {"access-1"}
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)

    def rotate_file_once(request: httpx.Request) -> None:
        if authorization(request) == "Bearer access-1":
            write_token_file(
                setup.token_dir,
                access_token="access-2",
                refresh_token="refresh-2",
                access_token_expires_at=START + 3610,
                obtained_at=START + 10,
            )

    zendesk.on_api = rotate_file_once

    server.get_ticket(1)

    assert setup.zendesk.api_authorizations() == ["Bearer access-1", "Bearer access-2"]
    assert setup.zendesk.token_requests == []


def test_stdio_auth_does_not_retry_403(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A 403 is a refusal, not a stale token: one bearer request, no refresh, the 403 surfaces."""
    zendesk = FakeZendesk()
    zendesk.api_status = lambda request: 403
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)

    with pytest.raises(httpx.HTTPStatusError) as raised:
        server.get_ticket(1)

    assert raised.value.response.status_code == 403
    assert setup.zendesk.api_authorizations() == ["Bearer access-1"]
    assert setup.zendesk.token_requests == []


def test_two_sessions_at_expiry_refresh_once(tmp_path: Path) -> None:
    """Two sessions (two stores on one file) at expiry refresh once and both requests succeed."""
    auth_class = getattr(server, "_StdioOAuth", None)
    assert auth_class is not None, "server.py must define _StdioOAuth"
    zendesk = FakeZendesk()
    token_dir = tmp_path / "zendesk-mcp"
    write_token_file(token_dir, access_token_expires_at=START + 60)
    clients = [httpx.Client(transport=httpx.MockTransport(zendesk)) for _ in range(2)]
    stores = [
        stdio_oauth.TokenStore(
            subdomain=SUBDOMAIN,
            client_id=CLIENT_ID,
            http_client=client,
            directory=token_dir,
            clock=FakeClock(),
        )
        for client in clients
    ]
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def session(index: int) -> None:
        barrier.wait()
        response = clients[index].get(
            f"{server.BASE_URL}/tickets/1.json", auth=auth_class(stores[index])
        )
        statuses.append(response.status_code)

    threads = [threading.Thread(target=session, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(zendesk.token_requests) == 1
    assert statuses == [200, 200]
    assert zendesk.api_authorizations() == ["Bearer access-2", "Bearer access-2"]


# ---------------------------------------------------------------------------
# Attachments, uploads and export over OAuth
# ---------------------------------------------------------------------------


def _stream_through(monkeypatch: pytest.MonkeyPatch, zendesk: FakeZendesk) -> None:
    """Send fetch_attachment's `httpx.stream` through a MockTransport client, so auth really runs."""
    stream_client = httpx.Client(transport=httpx.MockTransport(zendesk))
    monkeypatch.setattr(server.httpx, "stream", stream_client.stream)


def test_fetch_attachment_oauth_does_not_buffer_body(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Over OAuth the attachment still streams: the size cap stops it before the body ends."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())
    _stream_through(monkeypatch, setup.zendesk)

    result = json.loads(server.fetch_attachment(ATTACHMENT_URL, max_size_mb=1))

    assert setup.zendesk.api_authorizations() == ["Bearer access-1"]
    assert result["kind"] == "too_large"
    assert setup.zendesk.attachment_chunks_sent < ATTACHMENT_CHUNK_COUNT


def test_fetch_attachment_oauth_401_then_streams(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After a 401 and one refresh, the retried attachment body is still streamed under the cap."""
    zendesk = FakeZendesk()
    zendesk.rejected_tokens = {"access-1"}
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)
    _stream_through(monkeypatch, zendesk)

    result = json.loads(server.fetch_attachment(ATTACHMENT_URL, max_size_mb=1))

    assert setup.zendesk.api_authorizations() == ["Bearer access-1", "Bearer access-2"]
    assert len(setup.zendesk.token_requests) == 1
    assert result["kind"] == "too_large"
    assert setup.zendesk.attachment_chunks_sent < ATTACHMENT_CHUNK_COUNT


def test_stdio_attach_with_oauth_replays_upload_after_401(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 401 on the upload refreshes and re-sends the same bytes; the PUT carries the new token."""
    module = load_management(tmp_path, monkeypatch)
    zendesk = FakeZendesk()
    zendesk.rejected_tokens = {"access-1"}
    use_oauth(module, monkeypatch, tmp_path, zendesk)
    attachment = tmp_path / "note.txt"
    attachment.write_bytes(b"pr3 upload bytes")
    arguments = {"ticket_id": 1, "file_paths": [str(attachment)], "comment": "files"}

    is_error, text = call_tool(module, "attach_file_to_ticket", arguments)

    uploads = [r for r in zendesk.api_requests if r.url.path.endswith("/uploads.json")]
    puts = [r for r in zendesk.api_requests if r.method == "PUT"]
    assert is_error is False, text
    assert [authorization(r) for r in uploads] == ["Bearer access-1", "Bearer access-2"]
    assert [r.content for r in uploads] == [b"pr3 upload bytes", b"pr3 upload bytes"]
    assert [authorization(r) for r in puts] == ["Bearer access-2"]
    assert len(zendesk.token_requests) == 1


def test_stdio_export_with_oauth(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """export_ticket_comments fetches with the bearer token and writes the file."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())
    output = tmp_path / "out" / "thread.json"

    result = json.loads(server.export_ticket_comments(7, str(output)))

    assert setup.zendesk.api_authorizations() == ["Bearer access-1"]
    assert result.get("comment_count") == 0
    assert json.loads(output.read_text()) == []


# ---------------------------------------------------------------------------
# Write scope and ACCESS_LEVEL
# ---------------------------------------------------------------------------


def test_stdio_write_refused_for_read_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read-only sign-in cannot write, even with ACCESS_LEVEL=management; nothing is sent."""
    module = load_management(tmp_path, monkeypatch)
    setup = use_oauth(module, monkeypatch, tmp_path, FakeZendesk(), scope="read")

    is_error, text = call_tool(module, "add_ticket_tags", {"ticket_id": 1, "tags": ["x"]})

    assert is_error is True, text
    assert "no `write` scope" in text
    assert LOGIN_COMMAND in text
    assert setup.zendesk.api_requests == []
    assert setup.zendesk.token_requests == []


def test_require_write_scope_read_only_token_sends_no_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_require_write_scope()` refuses a read-only token file before any request."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), scope="read")

    with pytest.raises(PermissionError, match=LOGIN_COMMAND):
        server._require_write_scope()

    assert setup.zendesk.api_requests == []
    assert setup.zendesk.token_requests == []


def test_stdio_write_allowed_for_write_scope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A `read write` sign-in writes with the bearer token."""
    module = load_management(tmp_path, monkeypatch)
    setup = use_oauth(module, monkeypatch, tmp_path, FakeZendesk(), scope="read write")

    is_error, text = call_tool(module, "add_ticket_tags", {"ticket_id": 1, "tags": ["x"]})

    assert is_error is False, text
    assert [r.method for r in setup.zendesk.api_requests] == ["GET", "PUT"]
    assert setup.zendesk.api_authorizations() == ["Bearer access-1", "Bearer access-1"]


def test_stdio_write_unchecked_on_api_token_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no token file, a write runs with the env basic auth and the fallback warning."""
    module = load_management(tmp_path, monkeypatch)
    setup = use_oauth(module, monkeypatch, tmp_path, FakeZendesk(), write_file=False)

    is_error, text = call_tool(module, "add_ticket_tags", {"ticket_id": 1, "tags": ["x"]})

    assert is_error is False, text
    assert setup.zendesk.api_authorizations() == [basic_header(ENV_BASIC)] * 2
    assert len(warning_lines(capsys.readouterr().err)) == 1


def test_list_tools_readonly_level_with_write_scope_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ACCESS_LEVEL stays the ceiling: a `write` token does not register write tools."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), scope="read write")

    names = {tool.name for tool in anyio.run(server.mcp.list_tools)}
    is_error, _ = call_tool(server, "add_ticket_tags", {"ticket_id": 1, "tags": ["x"]})

    assert server.ACCESS_LEVEL == "readonly"
    assert names.isdisjoint({"add_ticket_tags", "create_ticket", "update_ticket"})
    assert is_error is True
    assert setup.zendesk.api_requests == []


# ---------------------------------------------------------------------------
# Fallback to the API token
# ---------------------------------------------------------------------------


def test_stdio_auth_falls_back_without_token_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No token file: the basic tuple, and one stderr warning naming the deadline and login."""
    use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)

    first = server._zendesk_auth()
    second = server._zendesk_auth()

    assert first == second == ENV_BASIC
    lines = warning_lines(capsys.readouterr().err)
    assert len(lines) == 1
    assert LOGIN_COMMAND in lines[0]
    assert "Set ZENDESK_STDIO_OAUTH_CLIENT_ID" not in lines[0]  # it is already set


def test_fallback_warning_printed_once_across_threads(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Eight threads falling back at once print the warning exactly once.

    The slow writer keeps each thread inside `print` long enough that, without the lock, every
    thread would pass the "already warned?" check before the first one sets the flag.
    """
    use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)
    monkeypatch.setattr(server.sys, "stderr", SlowWriter())
    barrier = threading.Barrier(8)

    def fall_back() -> None:
        barrier.wait()
        server._zendesk_auth()

    threads = [threading.Thread(target=fall_back) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert server.sys.stderr.text().count("2027-04-30") == 1


def _corrupt_json(token_file: Path) -> None:
    token_file.write_text("{not json")


def _open_permissions(token_file: Path) -> None:
    token_file.chmod(0o644)


@pytest.mark.parametrize("spoil", [_corrupt_json, _open_permissions], ids=["bad-json", "mode-0644"])
def test_stdio_auth_corrupt_file_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    spoil: Callable[[Path], None],
) -> None:
    """An unusable token file is an error; the server never quietly uses the API key instead."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())
    spoil(setup.token_dir / "tokens.json")

    with pytest.raises(stdio_oauth.TokenStoreError):
        server.get_ticket(1)

    assert setup.zendesk.api_requests == []
    assert warning_lines(capsys.readouterr().err) == []


def test_stdio_auth_other_account_does_not_fall_back(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A token file for another subdomain asks for a new sign-in, with no request."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), subdomain="other")

    with pytest.raises(stdio_oauth.LoginRequiredError):
        server.get_ticket(1)

    assert setup.zendesk.api_requests == []


@pytest.mark.parametrize(
    "email,api_key",
    [(None, None), ("env@example.com", None), (None, "env-key")],
    ids=["neither", "email-only", "key-only"],
)
def test_stdio_auth_without_any_credentials_names_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, email: str | None, api_key: str | None
) -> None:
    """Client ID set, no token file and no full email/key pair: the error says to run login."""
    use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)
    monkeypatch.setattr(server, "EMAIL", email)
    monkeypatch.setattr(server, "API_KEY", api_key)

    with pytest.raises(stdio_oauth.LoginRequiredError) as raised:
        server._zendesk_auth()

    assert stdio_oauth.LOGIN_HINT in str(raised.value)


def test_stdio_auth_login_mid_session_switches_to_oauth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sign-in during a session takes effect on the next call, with no restart."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)

    server.get_ticket(1)
    write_token_file(setup.token_dir)
    server.get_ticket(1)

    assert setup.zendesk.api_authorizations() == [basic_header(ENV_BASIC), "Bearer access-1"]


def test_stdio_without_fcntl_uses_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no `fcntl` (Windows) no store is built; auth and the write check use the fallback."""
    monkeypatch.setattr(stdio_oauth, "fcntl", None)
    module = load_copy(tmp_path, monkeypatch)

    assert "_STDIO_TOKEN_STORE" in vars(module)
    assert module._STDIO_TOKEN_STORE is None
    assert module._zendesk_auth() == ENV_BASIC
    assert module._require_write_scope() is None


# ---------------------------------------------------------------------------
# A dead sign-in gives one "run login" error
# ---------------------------------------------------------------------------


def test_stdio_dead_refresh_token_one_error_names_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An expiring token with a dead refresh token: one login error, one POST, no API request."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk, access_token_expires_at=START + 60)

    is_error, text = call_tool(server, "get_ticket", {"ticket_id": 1})

    assert is_error is True, text
    assert stdio_oauth.LOGIN_HINT in text
    assert len(setup.zendesk.token_requests) == 1
    assert setup.zendesk.api_requests == []

    call_tool(server, "get_ticket", {"ticket_id": 1})

    assert len(setup.zendesk.token_requests) <= 2
    assert setup.zendesk.api_requests == []


def test_bulk_comments_dead_sign_in_fails_before_fan_out(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The bulk tool checks the sign-in once, before its workers: one POST and one error."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk, access_token_expires_at=START + 60)

    is_error, text = call_tool(server, "get_comments_for_tickets", {"ticket_ids": [1, 2, 3]})

    assert is_error is True, text
    assert text.count(LOGIN_COMMAND) == 1
    assert len(setup.zendesk.token_requests) == 1
    assert setup.zendesk.api_requests == []


def test_stdio_revoked_fresh_token_one_error_names_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A revoked sign-in whose token looks fresh: 401, one refresh (`invalid_grant`), one error."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    zendesk.api_status = lambda request: 401
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)

    is_error, text = call_tool(server, "get_ticket", {"ticket_id": 1})

    assert is_error is True, text
    assert LOGIN_COMMAND in text
    assert len(setup.zendesk.api_requests) == 1
    assert len(setup.zendesk.token_requests) == 1

    with pytest.raises(stdio_oauth.LoginRequiredError) as raised:
        server.get_ticket(1)

    assert raised.value.__context__ is None


def _comments_status_by_ticket(statuses: dict[str, int]) -> Callable[[httpx.Request], int]:
    """Answer each ticket's comments request with its own status (200 when not listed)."""

    def status_for(request: httpx.Request) -> int:
        ticket_id = request.url.path.split("/tickets/")[1].split("/")[0]
        return statuses.get(ticket_id, 200)

    return status_for


def test_bulk_comments_revoked_fresh_token_one_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Revoked sign-in in the bulk tool: one error, no per-ticket map, no valid token spent."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    zendesk.api_status = lambda request: 401
    setup = use_oauth(server, monkeypatch, tmp_path, zendesk)
    file_before = (setup.token_dir / "tokens.json").read_bytes()

    is_error, text = call_tool(server, "get_comments_for_tickets", {"ticket_ids": [1, 2, 3]})

    assert is_error is True, text
    assert text.count(LOGIN_COMMAND) == 1
    assert "comments_by_ticket" not in text
    assert '"errors"' not in text
    refresh_tokens = [form_fields(r)["refresh_token"] for r in setup.zendesk.token_requests]
    assert 1 <= len(refresh_tokens) <= 3
    assert set(refresh_tokens) == {"refresh-1"}
    assert (setup.token_dir / "tokens.json").read_bytes() == file_before


def test_bulk_comments_partial_success_keeps_results_and_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If any ticket succeeded, the answer keeps its results and maps the login errors per ticket."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    zendesk.api_status = _comments_status_by_ticket({"2": 401, "3": 401})
    use_oauth(server, monkeypatch, tmp_path, zendesk)

    result = json.loads(server.get_comments_for_tickets([1, 2, 3]))

    assert result.get("comments_by_ticket") == {"1": []}
    errors = result.get("errors", {})
    assert sorted(errors) == ["2", "3"]
    assert all(LOGIN_COMMAND in text for text in errors.values())


def test_bulk_comments_mixed_errors_keep_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no success, a login error next to another error keeps the per-ticket map."""
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    zendesk.api_status = _comments_status_by_ticket({"1": 401, "2": 404})
    use_oauth(server, monkeypatch, tmp_path, zendesk)

    result = json.loads(server.get_comments_for_tickets([1, 2]))

    assert result.get("comments_by_ticket") == {}
    errors = result.get("errors", {})
    assert LOGIN_COMMAND in errors.get("1", "")
    assert "404" in errors.get("2", "")


def test_bulk_comments_empty_list_is_not_a_login_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No tickets means no failures: the single sign-in error needs at least one failed ticket."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())

    raised: Exception | None = None
    answer = "{}"
    try:
        answer = server.get_comments_for_tickets([])
    except stdio_oauth.LoginRequiredError as error:
        raised = error

    assert raised is None
    assert json.loads(answer).get("comments_by_ticket") == {}
    assert setup.zendesk.api_requests == []


def _expired_refresh(zendesk: FakeZendesk) -> dict:
    zendesk.refresh_mode = "invalid_grant"
    return {"access_token_expires_at": START + 60}


def _refresh_server_error(zendesk: FakeZendesk) -> dict:
    zendesk.refresh_mode = "server_error"
    zendesk.api_status = lambda request: 401
    return {}


def _revoked_bulk(zendesk: FakeZendesk) -> dict:
    zendesk.refresh_mode = "invalid_grant"
    zendesk.api_status = lambda request: 401
    return {}


@pytest.mark.parametrize(
    "arrange,call,expected",
    [
        (_expired_refresh, lambda: server.get_ticket(1), stdio_oauth.LoginRequiredError),
        (_refresh_server_error, lambda: server.get_ticket(1), stdio_oauth.TokenRefreshError),
        (
            _revoked_bulk,
            lambda: server.get_comments_for_tickets([1, 2]),
            stdio_oauth.LoginRequiredError,
        ),
    ],
    ids=["expired-refresh", "refresh-server-error", "bulk-all-login"],
)
def test_stdio_auth_errors_have_no_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    arrange: Callable[[FakeZendesk], dict],
    call: Callable[[], object],
    expected: type[Exception],
) -> None:
    """Sign-in errors are raised outside any `except` block, so they chain no other exception."""
    zendesk = FakeZendesk()
    token_overrides = arrange(zendesk)
    use_oauth(server, monkeypatch, tmp_path, zendesk, **token_overrides)

    with pytest.raises(expected) as raised:
        call()

    assert raised.value.__context__ is None
    assert raised.value.__cause__ is None


def test_stdio_auth_never_leaks_tokens(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """No token shows up in a repr, an exception, a tool error, or captured output and logs."""
    caplog.set_level("DEBUG")
    secrets = ["SECRET-ACCESS-abc123", "SECRET-REFRESH-xyz789"]
    zendesk = FakeZendesk(refresh_mode="invalid_grant")
    zendesk.api_status = lambda request: 401
    setup = use_oauth(
        server,
        monkeypatch,
        tmp_path,
        zendesk,
        access_token=secrets[0],
        refresh_token=secrets[1],
    )
    auth_class = getattr(server, "_StdioOAuth", None)
    assert auth_class is not None, "server.py must define _StdioOAuth"
    auth = auth_class(setup.store)

    _, tool_text = call_tool(server, "get_ticket", {"ticket_id": 1})
    with pytest.raises(stdio_oauth.LoginRequiredError) as raised:
        server.get_ticket(1)
    captured = capsys.readouterr()

    assert repr(auth) == "_StdioOAuth(<redacted>)"
    texts = [repr(auth), repr(setup.store), str(raised.value), repr(raised.value), tool_text]
    texts += [captured.out, captured.err, caplog.text]
    for text in texts:
        for secret in secrets:
            assert secret not in text
    assert zendesk.api_authorizations() == [f"Bearer {secrets[0]}"] * 2


# ---------------------------------------------------------------------------
# Import-time config
# ---------------------------------------------------------------------------


def test_stdio_imports_with_client_id_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stdio imports with only the OAuth client ID, and its store uses the shared client."""
    load_error: Exception | None = None
    module = None
    try:
        module = load_copy(tmp_path, monkeypatch, ZENDESK_EMAIL=None, ZENDESK_API_KEY=None)
    except KeyError as error:
        load_error = error

    assert load_error is None, f"stdio must import with only the client ID: {load_error!r}"
    store = getattr(module, "_STDIO_TOKEN_STORE", None)
    assert store is not None
    assert store._http_client is module._client


@pytest.mark.parametrize(
    "env",
    [
        {"ZENDESK_EMAIL": None},
        {"ZENDESK_API_KEY": None},
        {"ZENDESK_EMAIL": None, "ZENDESK_API_KEY": None},
        {"ZENDESK_STDIO_OAUTH_CLIENT_ID": "", "ZENDESK_EMAIL": None, "ZENDESK_API_KEY": None},
    ],
    ids=["no-email", "no-key", "neither", "blank-id-neither"],
)
def test_stdio_import_needs_client_id_or_key_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, env: dict[str, str | None]
) -> None:
    """With no (or a blank) client ID, stdio needs both the email and the key."""
    with pytest.raises(KeyError, match="ZENDESK_STDIO_OAUTH_CLIENT_ID"):
        load_copy(tmp_path, monkeypatch, **{"ZENDESK_STDIO_OAUTH_CLIENT_ID": None, **env})


def test_stdio_without_client_id_builds_no_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """With no OAuth client ID, stdio builds no store and runs on the email/key pair."""
    module = load_copy(tmp_path, monkeypatch, ZENDESK_STDIO_OAUTH_CLIENT_ID=None)

    assert "_STDIO_TOKEN_STORE" in vars(module)
    assert module._STDIO_TOKEN_STORE is None
    assert module._zendesk_auth() == ENV_BASIC
    lines = warning_lines(capsys.readouterr().err)
    assert len(lines) == 1
    assert "Set ZENDESK_STDIO_OAUTH_CLIENT_ID" in lines[0]


def test_http_mode_builds_no_token_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HTTP mode never builds the stdio token store."""
    module = load_copy(tmp_path, monkeypatch, MCP_TRANSPORT="streamable-http")

    assert "_STDIO_TOKEN_STORE" in vars(module)
    assert module._STDIO_TOKEN_STORE is None


# ---------------------------------------------------------------------------
# Token-directory guard
# ---------------------------------------------------------------------------


def _token_dir_contents(token_dir: Path) -> None:
    """Fill the token dir with the store's files: the token file, the lock and a leftover temp."""
    write_token_file(token_dir)
    (token_dir / "tokens.lock").write_text("")
    (token_dir / ".tokens.abc.tmp").write_text("{}")


def _symlinked_dir(tmp_path: Path, token_dir: Path) -> Path:
    link = tmp_path / "link"
    link.symlink_to(token_dir, target_is_directory=True)
    return link / "tokens.json"


def _dot_dot(tmp_path: Path, token_dir: Path) -> Path:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)
    return elsewhere / ".." / token_dir.name / "tokens.json"


GUARDED_PATHS: dict[str, Callable[[Path, Path], Path]] = {
    "tokens.json": lambda tmp_path, token_dir: token_dir / "tokens.json",
    "tokens.lock": lambda tmp_path, token_dir: token_dir / "tokens.lock",
    "leftover-tmp": lambda tmp_path, token_dir: token_dir / ".tokens.abc.tmp",
    "symlinked-dir": _symlinked_dir,
    "dot-dot": _dot_dot,
}
EXPORT_ONLY_PATHS: dict[str, Callable[[Path, Path], Path]] = {
    "new-file": lambda tmp_path, token_dir: token_dir / "new.json",
    "new-subdir": lambda tmp_path, token_dir: token_dir / "sub" / "x.json",
}


def _snapshot(directory: Path) -> dict[str, bytes]:
    """Return every file under `directory` with its bytes."""
    return {
        str(path.relative_to(directory)): path.read_bytes() if path.is_file() else b"<dir>"
        for path in sorted(directory.rglob("*"))
    }


@pytest.mark.parametrize("mode", ["oauth", "fallback"])
@pytest.mark.parametrize("kind", list(GUARDED_PATHS))
def test_attach_refuses_token_dir_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, mode: str
) -> None:
    """attach_file_to_ticket refuses any file in the token dir, in both auth modes, sending nothing."""
    module = load_management(tmp_path, monkeypatch)
    setup = use_oauth(module, monkeypatch, tmp_path, FakeZendesk(), with_store=mode == "oauth")
    _token_dir_contents(setup.token_dir)
    path = GUARDED_PATHS[kind](tmp_path, setup.token_dir)

    with pytest.raises(ValueError) as raised:
        module.attach_file_to_ticket(1, [str(path)], "note")

    assert "Nothing was uploaded" in str(raised.value)
    assert "token directory" in str(raised.value)
    assert setup.zendesk.api_requests == []


@pytest.mark.parametrize("mode", ["oauth", "fallback"])
@pytest.mark.parametrize("kind", list(GUARDED_PATHS) + list(EXPORT_ONLY_PATHS))
def test_export_refuses_token_dir_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str, mode: str
) -> None:
    """export_ticket_comments refuses an output_path in the token dir and changes nothing there."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), with_store=mode == "oauth")
    _token_dir_contents(setup.token_dir)
    path = {**GUARDED_PATHS, **EXPORT_ONLY_PATHS}[kind](tmp_path, setup.token_dir)
    before = _snapshot(setup.token_dir)

    result = json.loads(server.export_ticket_comments(7, str(path)))

    assert "token directory" in result.get("error", "")
    assert setup.zendesk.api_requests == []
    assert _snapshot(setup.token_dir) == before


def test_token_dir_guard_case_variant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """On a case-insensitive disk, an upper-cased path to the token dir is refused too."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk())
    variant_dir = setup.token_dir.parent / setup.token_dir.name.upper()
    if not variant_dir.exists():
        pytest.skip("the temp filesystem is case-sensitive")
    before = _snapshot(setup.token_dir)

    result = json.loads(server.export_ticket_comments(7, str(variant_dir / "x.json")))

    assert "token directory" in result.get("error", "")
    assert setup.zendesk.api_requests == []
    assert _snapshot(setup.token_dir) == before


@pytest.mark.parametrize(
    "relative", ["tokens.json", "sub/x.json"], ids=["token-file", "new-subdir"]
)
def test_export_refuses_token_dir_paths_before_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, relative: str
) -> None:
    """Before any sign-in the token dir does not exist yet; export still refuses to create it."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)

    result = json.loads(server.export_ticket_comments(7, str(setup.token_dir / relative)))

    assert "token directory" in result.get("error", "")
    assert setup.zendesk.api_requests == []
    assert not setup.token_dir.exists()


def _case_insensitive_disk(directory: Path) -> bool:
    """Return True when `directory` sits on a case-insensitive filesystem."""
    probe = directory / "case-probe"
    probe.write_text("")
    return (directory / "CASE-PROBE").exists()


def test_export_refuses_case_variant_before_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no token dir yet, an upper-cased token dir name is refused on a case-insensitive disk."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)
    if not _case_insensitive_disk(tmp_path):
        pytest.skip("the temp filesystem is case-sensitive")
    variant_dir = setup.token_dir.parent / setup.token_dir.name.upper()

    result = json.loads(server.export_ticket_comments(7, str(variant_dir / "tokens.json")))

    assert "token directory" in result.get("error", "")
    assert setup.zendesk.api_requests == []
    assert not setup.token_dir.exists()


def test_export_refuses_firmlink_form_before_dir_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With no token dir yet, the macOS `/System/Volumes/Data<path>` form is refused too."""
    setup = use_oauth(server, monkeypatch, tmp_path, FakeZendesk(), write_file=False)
    firmlink_parent = Path("/System/Volumes/Data" + str(setup.token_dir.parent.resolve()))
    if not firmlink_parent.is_dir():
        pytest.skip("no /System/Volumes/Data form of the temp dir on this system")

    output = firmlink_parent / setup.token_dir.name / "tokens.json"
    result = json.loads(server.export_ticket_comments(7, str(output)))

    assert "token directory" in result.get("error", "")
    assert setup.zendesk.api_requests == []
    assert not setup.token_dir.exists()


def test_token_dir_guard_without_token_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With no token dir on disk there is nothing to guard: normal attach and export work."""
    module = load_management(tmp_path, monkeypatch)
    setup = use_oauth(module, monkeypatch, tmp_path, FakeZendesk(), write_file=False)
    use_oauth(server, monkeypatch, tmp_path, setup.zendesk, write_file=False)
    guard = getattr(server, "_in_token_directory", None)
    assert guard is not None, "server.py must define _in_token_directory"
    attachment = tmp_path / "note.txt"
    attachment.write_text("hello")
    output = tmp_path / "out" / "thread.json"

    assert not setup.token_dir.exists()
    assert guard(output) is False
    module.attach_file_to_ticket(1, [str(attachment)], "note")
    result = json.loads(server.export_ticket_comments(7, str(output)))

    assert result.get("comment_count") == 0
    assert output.exists()
    assert not setup.token_dir.exists()


# ---------------------------------------------------------------------------
# Test-suite isolation
# ---------------------------------------------------------------------------


def test_suite_token_dir_is_not_real_home() -> None:
    """The suite's token dir is under the temp HOME from conftest, never the real home."""
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    token_dir = getattr(server, "_STDIO_TOKEN_DIR", None)
    assert token_dir is not None, "server.py must define _STDIO_TOKEN_DIR"

    for directory in (Path(token_dir), stdio_oauth.DEFAULT_TOKEN_DIR):
        assert real_home not in directory.resolve().parents


def test_leftover_token_file_is_removed_and_reported(tmp_path: Path) -> None:
    """conftest removes a token file a test left in the shared test HOME, and only there."""
    conftest = importlib.import_module("conftest")
    remove_leftover = getattr(conftest, "remove_leftover_token_file", None)
    assert remove_leftover is not None, "conftest must define remove_leftover_token_file"
    leftover = write_token_file(stdio_oauth.DEFAULT_TOKEN_DIR)
    outside = write_token_file(tmp_path / "not-test-home" / "zendesk-mcp")

    assert remove_leftover(stdio_oauth.DEFAULT_TOKEN_DIR) is True
    assert not leftover.exists()
    assert remove_leftover(stdio_oauth.DEFAULT_TOKEN_DIR) is False
    assert remove_leftover(outside.parent) is False
    assert outside.exists()
