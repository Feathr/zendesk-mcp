"""Offline tests for per-request Zendesk auth and the fetch_attachment guards.

HTTP is stubbed: helpers run against an `httpx.MockTransport`, and `fetch_attachment` against a
fake `httpx.stream`. No real Zendesk instance or `.env` is needed.
"""

import base64
import contextlib
import contextvars
import importlib.util
import json
import shutil
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import anyio
import httpx
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import server

SENTINEL = ("sentinel@example.com/token", "sentinel-key")
WORKER_AUTH = ("worker@example.com/token", "worker-key")
ATTACHMENT_URL = "https://example.zendesk.com/attachments/token/abc/?name=a.txt"


def _basic_header(credentials: tuple[str, str]) -> str:
    """Return the Authorization header value httpx sends for a basic-auth tuple."""
    username, password = credentials
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _sentinel_auth() -> tuple[str, str]:
    """Stand-in for `server._zendesk_auth` that returns the sentinel credentials."""
    return SENTINEL


@pytest.fixture
def sent_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Replace `server._client` with a stub that records every request it sends.

    The stub client carries its own "module" credentials, so a helper that relies on client-level
    auth sends those instead of the patched `_zendesk_auth()` value.
    """
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request)
        if request.url.path.endswith("/uploads.json"):
            return httpx.Response(201, json={"upload": {"token": "t"}})
        return httpx.Response(200, json={"comments": [], "next_page": None})

    stub_client = httpx.Client(
        base_url=server.BASE_URL,
        headers={"Accept": "application/json"},
        auth=("module@example.com/token", "module-key"),
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr(server, "_client", stub_client)
    return recorded


def _call_get() -> object:
    """Call `_get` once."""
    return server._get("/tickets/1.json")


def _call_get_with_retry() -> object:
    """Call `_get_with_retry` once."""
    return server._get_with_retry("/tickets/1.json", deadline=None)


def _call_post() -> object:
    """Call `_post` once."""
    return server._post("/tickets.json", {"ticket": {}})


def _call_put() -> object:
    """Call `_put` once."""
    return server._put("/tickets/1.json", {"ticket": {}})


def _call_delete() -> object:
    """Call `_delete` once."""
    return server._delete("/tickets/1.json")


def _call_post_upload() -> object:
    """Call `_post_upload` once."""
    return server._post_upload("a.txt", b"hello", "text/plain")


@pytest.mark.parametrize(
    "call_helper",
    [_call_get, _call_get_with_retry, _call_post, _call_put, _call_delete, _call_post_upload],
    ids=["_get", "_get_with_retry", "_post", "_put", "_delete", "_post_upload"],
)
def test_helpers_send_zendesk_auth(
    monkeypatch: pytest.MonkeyPatch,
    sent_requests: list[httpx.Request],
    call_helper: Callable[[], object],
) -> None:
    """Each HTTP helper sends the credentials from `_zendesk_auth()`, not the client's."""
    monkeypatch.setattr(server, "_zendesk_auth", _sentinel_auth, raising=False)

    call_helper()

    assert len(sent_requests) == 1
    assert sent_requests[0].headers.get("authorization") == _basic_header(SENTINEL)


def test_zendesk_auth_returns_basic_tuple() -> None:
    """`_zendesk_auth()` returns the env basic-auth tuple, as stdio sends today."""
    zendesk_auth = getattr(server, "_zendesk_auth", lambda: None)

    assert zendesk_auth() == ("env@example.com/token", "env-key")


def test_client_has_no_default_auth() -> None:
    """The module client holds no credentials; every request supplies its own."""
    assert server._client.auth is None


def test_get_comments_for_tickets_resolves_auth_in_caller_thread(
    monkeypatch: pytest.MonkeyPatch, sent_requests: list[httpx.Request]
) -> None:
    """Workers send the auth resolved in the calling thread, not their own."""
    caller_thread_id = threading.get_ident()

    def thread_aware_auth() -> tuple[str, str]:
        if threading.get_ident() == caller_thread_id:
            return SENTINEL
        return WORKER_AUTH

    monkeypatch.setattr(server, "_zendesk_auth", thread_aware_auth, raising=False)

    server.get_comments_for_tickets([1, 2, 3], max_concurrency=3)

    assert len(sent_requests) == 3
    sent_headers = {request.headers.get("authorization") for request in sent_requests}
    assert sent_headers == {_basic_header(SENTINEL)}


def test_get_comments_for_tickets_passes_caller_auth(
    monkeypatch: pytest.MonkeyPatch, sent_requests: list[httpx.Request]
) -> None:
    """A contextvar set by the caller reaches the workers' requests.

    Under `-X thread_inherit_context=1` (free-threaded default) workers inherit the context, so
    this test stops discriminating there; the thread-ident test above still does.
    """
    caller_auth_var: contextvars.ContextVar[tuple[str, str]] = contextvars.ContextVar(
        "caller_auth", default=WORKER_AUTH
    )

    def contextvar_auth() -> tuple[str, str]:
        return caller_auth_var.get()

    monkeypatch.setattr(server, "_zendesk_auth", contextvar_auth, raising=False)
    token = caller_auth_var.set(SENTINEL)
    try:
        server.get_comments_for_tickets([1, 2, 3], max_concurrency=3)
    finally:
        caller_auth_var.reset(token)

    assert len(sent_requests) == 3
    sent_headers = {request.headers.get("authorization") for request in sent_requests}
    assert sent_headers == {_basic_header(SENTINEL)}


def _fake_stream_returning(
    response: httpx.Response, calls: list[dict]
) -> Callable[..., contextlib.AbstractContextManager[httpx.Response]]:
    """Build a stand-in for `httpx.stream` that records its call and yields `response`."""

    @contextlib.contextmanager
    def fake_stream(method: str, url: str, **kwargs: object) -> Iterator[httpx.Response]:
        calls.append({"method": method, "url": url, **kwargs})
        response.request = httpx.Request(method, url)
        yield response

    return fake_stream


def test_fetch_attachment_sends_zendesk_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    """`fetch_attachment` sends `_zendesk_auth()` and keeps no-redirects and its 60 s timeout."""
    calls: list[dict] = []
    response = httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hi")
    monkeypatch.setattr(server.httpx, "stream", _fake_stream_returning(response, calls))
    monkeypatch.setattr(server, "_zendesk_auth", _sentinel_auth, raising=False)

    result = server.fetch_attachment(ATTACHMENT_URL)

    assert result == "hi"
    assert len(calls) == 1
    assert calls[0]["auth"] == SENTINEL
    assert calls[0]["follow_redirects"] is False
    assert calls[0]["timeout"] == 60.0


def _refuse_stream(*args: object, **kwargs: object) -> None:
    """Stand-in for `httpx.stream` that fails the test if any request is attempted."""
    raise AssertionError("fetch_attachment sent a request for a rejected URL")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.zendesk.com/a",
        "https://evil.com/a",
        "https://example.zendesk.com.evil.com/a",
        "https://evil.example.zendesk.com/a",
    ],
    ids=["http", "other_host", "suffix_host", "sub_subdomain"],
)
def test_fetch_attachment_rejects_off_subdomain(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    """The SSRF guard rejects non-HTTPS and off-subdomain URLs without sending a request."""
    monkeypatch.setattr(server.httpx, "stream", _refuse_stream)

    result = json.loads(server.fetch_attachment(url))

    assert "error" in result


def test_fetch_attachment_returns_redirect_unfollowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirect is returned as `kind: redirect` with its location, not followed."""
    calls: list[dict] = []
    response = httpx.Response(302, headers={"location": "https://cdn.example.net/file"})
    monkeypatch.setattr(server.httpx, "stream", _fake_stream_returning(response, calls))

    result = json.loads(server.fetch_attachment(ATTACHMENT_URL))

    assert result["kind"] == "redirect"
    assert result["location"] == "https://cdn.example.net/file"
    assert len(calls) == 1


def test_fetch_attachment_caps_declared_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A declared Content-Length over the cap returns `too_large` before reading the body."""
    calls: list[dict] = []
    two_mib = str(2 * 1024 * 1024)
    response = httpx.Response(
        200, headers={"content-type": "text/plain", "content-length": two_mib}, content=b""
    )
    monkeypatch.setattr(server.httpx, "stream", _fake_stream_returning(response, calls))

    result = json.loads(server.fetch_attachment(ATTACHMENT_URL, max_size_mb=1))

    assert result["kind"] == "too_large"


def _one_mib_plus_one_byte() -> Iterator[bytes]:
    """Yield a body of 1 MiB + 1 byte, with no Content-Length."""
    yield b"x" * (1024 * 1024)
    yield b"x"


def test_fetch_attachment_caps_streamed_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """A streamed body over the cap returns `too_large` even without Content-Length."""
    calls: list[dict] = []
    response = httpx.Response(
        200, headers={"content-type": "text/plain"}, content=_one_mib_plus_one_byte()
    )
    monkeypatch.setattr(server.httpx, "stream", _fake_stream_returning(response, calls))

    result = json.loads(server.fetch_attachment(ATTACHMENT_URL, max_size_mb=1))

    assert result["kind"] == "too_large"


def test_client_keeps_timeout_and_accept_header() -> None:
    """The real module client keeps its 30 s timeout and JSON Accept header."""
    assert server._client.timeout == httpx.Timeout(30.0)
    assert server._client.headers.get("accept") == "application/json"


def test_post_upload_sends_upload_timeout(sent_requests: list[httpx.Request]) -> None:
    """`_post_upload` sends its request with the 120 s upload timeout."""
    server._post_upload("a.txt", b"hello", "text/plain")

    assert len(sent_requests) == 1
    assert sent_requests[0].extensions["timeout"] == httpx.Timeout(120.0).as_dict()


async def _call_tool_in_memory(module: ModuleType, name: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool through an in-memory MCP session and return (isError, first text content)."""
    async with create_connected_server_and_client_session(module.mcp) as session:
        result = await session.call_tool(name, arguments)
    return result.isError, result.content[0].text


def test_require_write_scope_noop_in_stdio(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """In stdio a write tool runs with the env basic auth; it needs no OAuth `write` scope."""
    shutil.copy(Path(server.__file__), tmp_path / "server.py")  # no real .env next to the copy
    monkeypatch.setenv("ACCESS_LEVEL", "management")
    spec = importlib.util.spec_from_file_location("server_management", tmp_path / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    recorded: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with a tagged ticket."""
        recorded.append(request)
        return httpx.Response(200, json={"ticket": {"id": 1, "tags": ["x"]}})

    stub_client = httpx.Client(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "_client", stub_client)

    arguments = {"ticket_id": 1, "tags": ["x"]}
    is_error, text = anyio.run(_call_tool_in_memory, module, "add_ticket_tags", arguments)

    assert is_error is False, text
    assert [request.method for request in recorded] == ["GET", "PUT"]
    expected_header = _basic_header(("env@example.com/token", "env-key"))
    assert {request.headers.get("authorization") for request in recorded} == {expected_header}
