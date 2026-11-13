"""Offline tests for the stdio/HTTP transport split; each test loads its own copy of server.py.

The copy lives in a temp dir, so no real `.env` is loaded. HTTP is served through Starlette's
`TestClient` and Zendesk is stubbed with `httpx.MockTransport`; nothing reaches the network.
"""

import ast
import base64
import contextlib
import importlib.util
import itertools
import json
import os
import re
import shutil
import threading
import time
import types
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

SERVER_FILE = Path(__file__).resolve().parent.parent / "server.py"
ZENDESK_FLOW_PATH = "/zendesk/mcp"
CLAUDE_CODE_PATH = "/claude/mcp"
BASE_URL = "http://127.0.0.1:8000"
CALLER_TOKEN = "caller-token-0123456789"
NO_AUTH_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
}
MCP_HEADERS = {**NO_AUTH_HEADERS, "authorization": f"Bearer {CALLER_TOKEN}"}
TOKEN_INFO = {"client_id": 7, "user_id": 42, "scopes": ["read"], "expires_at": None}
HTTP_WRITE_SCOPE_NOTE = (
    "Over HTTP this tool needs an OAuth token with the `write` scope. Tokens issued through "
    "/zendesk/mcp normally have only `read`, so the tool refuses them."
)
GET_TICKET_CALL = {"name": "get_ticket", "arguments": {"ticket_id": 1}}
ADMIN_TOOL_NAMES = """add_ticket_comment add_ticket_tags attach_file_to_ticket bulk_update_tickets
count_tickets create_organization create_ticket create_user delete_ticket export_search_results
export_ticket_comments fetch_attachment get_article get_comments_for_tickets get_organization
get_ticket get_ticket_audits get_ticket_comments get_user get_view_tickets
list_categories_and_sections list_content_tags list_user_segments list_views merge_tickets
remove_ticket_tags search_articles search_organizations search_tickets search_users
update_organization update_ticket update_user
""".split()  # noqa: SIM905  # compact 33-name stdio guard; a list literal adds ~28 lines
_module_ids = itertools.count()


@pytest.fixture
def load_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., ModuleType]:
    """Return a loader that imports a fresh server.py copy under the given env overrides.

    Every `MCP_*` and `FASTMCP_*` var is cleared first. A `None` value deletes a var, and
    `run_as_main=True` executes the copy as `__main__`.
    """
    shutil.copy(SERVER_FILE, tmp_path / "server.py")
    for name in list(os.environ):
        if name.startswith(("MCP_", "FASTMCP_")):
            monkeypatch.delenv(name)

    def load(run_as_main: bool = False, **env: str | None) -> ModuleType:
        """Apply `env` and import the copy, as `__main__` when `run_as_main` is set."""
        for name, value in env.items():
            if value is None:
                monkeypatch.delenv(name, raising=False)
            else:
                monkeypatch.setenv(name, value)
        module_name = "__main__" if run_as_main else f"server_copy_{next(_module_ids)}"
        spec = importlib.util.spec_from_file_location(module_name, tmp_path / "server.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    return load


def _load_http(load_server: Callable[..., ModuleType], **env: str | None) -> ModuleType:
    """Load server.py in HTTP mode and check that it built the Starlette `app`."""
    load_error: Exception | None = None
    try:
        module = load_server(MCP_TRANSPORT="streamable-http", **env)
    except (KeyError, RuntimeError) as error:
        load_error = error
    assert load_error is None, f"HTTP mode must start with this config: {load_error!r}"
    assert isinstance(getattr(module, "app", None), Starlette), "HTTP mode must build `app`"
    module._client = _zendesk_stub_client(module, [], [])
    return module


def _module_attr(module: ModuleType, name: str) -> Any:
    """Return a module attribute, failing on an assertion (not AttributeError) when it is missing."""
    value = getattr(module, name, None)
    assert value is not None, f"server.py must define {name}"
    return value


def _zendesk_stub_client(
    module: ModuleType, seen: list[httpx.Request], token_checks: list[httpx.Request]
) -> httpx.Client:
    """Build a stub Zendesk client: `tokens/current` accepts only CALLER_TOKEN; others get a ticket."""

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer a token check from TOKEN_INFO, or record the request and return a ticket."""
        if request.url.path.endswith("/oauth/tokens/current.json"):
            token_checks.append(request)
            if request.headers.get("authorization") != f"Bearer {CALLER_TOKEN}":
                return httpx.Response(401, json={"error": "invalid_token"})
            return httpx.Response(200, json={"token": TOKEN_INFO})
        seen.append(request)
        return httpx.Response(200, json={"ticket": {"id": 1}, "comments": [], "next_page": None})

    return httpx.Client(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))


def _tool_names(module: ModuleType) -> list[str]:
    """Return the names of the tools registered on the module's FastMCP server."""
    return [tool.name for tool in anyio.run(module.mcp.list_tools)]


def _description_lines(module: ModuleType, tool_name: str) -> list[str]:
    """Return the stripped lines of a registered tool's description."""
    tools = {tool.name: tool for tool in anyio.run(module.mcp.list_tools)}
    return [line.strip() for line in tools[tool_name].description.splitlines()]


def _rpc(
    client: TestClient, path: str, method: str, params: dict | None = None, **headers: str
) -> httpx.Response:
    """POST one JSON-RPC request to an MCP path."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return client.post(path, json=body, headers={**MCP_HEADERS, **headers})


def _stub_zendesk(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Replace the module's Zendesk client with a stub and return the requests it receives."""
    seen: list[httpx.Request] = []
    monkeypatch.setattr(module, "_client", _zendesk_stub_client(module, seen, []))
    return seen


def _record_launches(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    """Stub `FastMCP.run` and `uvicorn.run`, and return the list of launches they record."""
    launches: list[tuple] = []

    def fake_mcp_run(self: FastMCP, transport: str = "stdio", **kwargs: object) -> None:
        """Record a FastMCP launch instead of serving."""
        launches.append(("mcp.run", transport))

    def fake_uvicorn_run(app: object, **kwargs: object) -> None:
        """Record a uvicorn launch instead of serving."""
        launches.append(("uvicorn.run", app, kwargs))

    monkeypatch.setattr(FastMCP, "run", fake_mcp_run)
    monkeypatch.setattr(uvicorn, "run", fake_uvicorn_run)
    return launches


def test_stdio_admin_tool_names_unchanged(load_server: Callable[..., ModuleType]) -> None:
    """Stdio admin mode registers exactly origin/main's tools."""
    assert sorted(_tool_names(load_server(ACCESS_LEVEL="admin"))) == ADMIN_TOOL_NAMES


@pytest.mark.parametrize(
    "transport,name",
    [
        ("stdio", "ZENDESK_SUBDOMAIN"),
        ("stdio", "ZENDESK_EMAIL"),
        ("stdio", "ZENDESK_API_KEY"),
        ("streamable-http", "ZENDESK_SUBDOMAIN"),
    ],
)
def test_missing_config_fails_at_import(
    load_server: Callable[..., ModuleType], transport: str, name: str
) -> None:
    """A missing required var raises KeyError at import."""
    with pytest.raises(KeyError, match=name):
        load_server(MCP_TRANSPORT=transport, **{name: None})


def test_stdio_comment_text_unchanged(load_server: Callable[..., ModuleType]) -> None:
    """Stdio keeps the export hint and the raw comment-tool docstrings."""
    module = load_server()
    hint = module._CommentThreadTooLarge(1, 0, None).to_error()["hint"]
    assert hint == "Use export_ticket_comments to write the thread to a local file."
    tools = {tool.name: tool for tool in anyio.run(module.mcp.list_tools)}
    for name in ("get_ticket_comments", "get_comments_for_tickets"):
        assert tools[name].description == getattr(module, name).__doc__
        assert "export_ticket_comments" in tools[name].description


def test_invalid_transport_fails_at_import(load_server: Callable[..., ModuleType]) -> None:
    """An unknown MCP_TRANSPORT raises ValueError at import."""
    with pytest.raises(ValueError, match="MCP_TRANSPORT"):
        load_server(MCP_TRANSPORT="sse")


def test_transport_value_is_case_insensitive(load_server: Callable[..., ModuleType]) -> None:
    """MCP_TRANSPORT is matched case-insensitively."""
    try:
        module = load_server(MCP_TRANSPORT="Streamable-HTTP")
    except ValueError as error:
        pytest.fail(f"MCP_TRANSPORT must be case-insensitive: {error}")
    assert module.TRANSPORT == "streamable-http"


def test_http_mode_imports_without_zendesk_credentials(
    load_server: Callable[..., ModuleType],
) -> None:
    """HTTP mode imports without creds and is stateless with JSON responses."""
    module = _load_http(load_server, ZENDESK_EMAIL=None, ZENDESK_API_KEY=None)
    assert module.mcp.settings.stateless_http and module.mcp.settings.json_response


@pytest.mark.parametrize(
    "host,refused",
    [
        ("0.0.0.0", True),
        ("10.0.0.5", True),
        ("127.0.0.1", False),
        ("localhost", False),
        ("::1", False),
    ],
)
def test_http_mode_host_guard(
    load_server: Callable[..., ModuleType], host: str, refused: bool
) -> None:
    """Without MCP_PUBLIC_URL, HTTP mode refuses a non-loopback MCP_HOST and loads on loopback."""
    if refused:
        with pytest.raises(RuntimeError, match="MCP_HOST"):
            load_server(MCP_TRANSPORT="streamable-http", MCP_HOST=host)
    else:
        _load_http(load_server, MCP_HOST=host)


@pytest.mark.parametrize("port", ["abc", "0", "65536"])
def test_http_mode_rejects_bad_port(load_server: Callable[..., ModuleType], port: str) -> None:
    """An MCP_PORT that is not a number in 1-65535 raises ValueError at import."""
    with pytest.raises(ValueError, match="MCP_PORT"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_PORT=port)


@pytest.mark.parametrize("level,count", [("readonly", 17), ("management", 22), ("admin", 31)])
def test_http_mode_omits_local_file_tools(
    load_server: Callable[..., ModuleType], level: str, count: int
) -> None:
    """HTTP mode does not register the two local-file tools."""
    names = _tool_names(load_server(MCP_TRANSPORT="streamable-http", ACCESS_LEVEL=level))
    assert "export_ticket_comments" not in names
    assert "attach_file_to_ticket" not in names
    assert len(names) == count


def test_http_endpoints_list_same_tools(load_server: Callable[..., ModuleType]) -> None:
    """Both endpoint paths list the same tools, and bare /mcp is 404."""
    module = _load_http(load_server)
    expected = _tool_names(module)
    with TestClient(module.app, base_url=BASE_URL) as client:
        for path in (ZENDESK_FLOW_PATH, CLAUDE_CODE_PATH):
            response = _rpc(client, path, "tools/list")
            assert response.status_code == 200
            assert [tool["name"] for tool in response.json()["result"]["tools"]] == expected
        assert _rpc(client, "/mcp", "tools/list").status_code == 404


def test_http_tool_call_sends_caller_token(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With env creds set, an HTTP tool call still sends only the caller's bearer token."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    with TestClient(module.app, base_url=BASE_URL) as client:
        result = _rpc(client, CLAUDE_CODE_PATH, "tools/call", GET_TICKET_CALL).json()["result"]
    assert '"id": 1' in result["content"][0]["text"]
    assert [request.headers["authorization"] for request in seen] == [f"Bearer {CALLER_TOKEN}"]


def test_http_health_and_host_check(load_server: Callable[..., ModuleType]) -> None:
    """/health answers 200; a foreign Host gets 421, a foreign Origin 403, loopback 200."""
    module = _load_http(load_server)
    with TestClient(module.app, base_url=BASE_URL) as client:
        health = client.get("/health")
        assert (health.status_code, health.json()) == (200, {"status": "ok"})
        assert _rpc(client, CLAUDE_CODE_PATH, "tools/list", host="evil.com").status_code == 421
        assert (
            _rpc(client, CLAUDE_CODE_PATH, "tools/list", host="127.0.0.1:8000").status_code == 200
        )
        response = _rpc(client, CLAUDE_CODE_PATH, "tools/list", origin="https://evil.com")
        assert response.status_code == 403
        response = _rpc(client, CLAUDE_CODE_PATH, "tools/list", origin="http://localhost:8000")
        assert response.status_code == 200


def test_http_comment_text_rewritten(load_server: Callable[..., ModuleType]) -> None:
    """HTTP mode drops every mention of export_ticket_comments from hints and descriptions."""
    module = load_server(MCP_TRANSPORT="streamable-http")
    hint = module._CommentThreadTooLarge(1, 0, None).to_error()["hint"]
    assert hint == (
        "This thread is too large to return inline, and exporting to a file is not available "
        "on this server."
    )
    tools = anyio.run(module.mcp.list_tools)
    assert not any("export_ticket_comments" in tool.description for tool in tools)
    assert "an `error` instead of being returned inline." in _description_lines(
        module, "get_ticket_comments"
    )
    assert (
        "A ticket whose thread is over ~1 MB lands in `errors`. If the combined response would "
        "exceed ~4 MB,"
    ) in _description_lines(module, "get_comments_for_tickets")


@pytest.mark.parametrize("doc", ["Without the phrase.", "one phrase here, and one phrase here."])
def test_http_doc_remove_requires_single_match(
    load_server: Callable[..., ModuleType], doc: str
) -> None:
    """In HTTP mode, a docstring rewrite whose phrase is absent or repeated fails loudly."""
    module = _load_http(load_server)

    def dummy() -> None:
        pass

    dummy.__doc__ = doc
    with pytest.raises(RuntimeError, match="one phrase here"):
        module._http_doc_remove("one phrase here", "")(dummy)


@pytest.mark.parametrize("doc", ["No section.", "Args:\n    a: one.\n\nArgs:\n    b: two."])
def test_http_doc_needs_write_scope_requires_single_args(
    load_server: Callable[..., ModuleType], doc: str
) -> None:
    """In HTTP mode, the write-scope note fails loudly unless the docstring has one `Args:` line."""
    module = _load_http(load_server)
    add_note = _module_attr(module, "_http_doc_needs_write_scope")

    def dummy() -> None:
        pass

    dummy.__doc__ = "Summary.\n\n" + doc
    with pytest.raises(RuntimeError, match="exactly one 'Args:' line"):
        add_note()(dummy)


@pytest.mark.parametrize("route_count", [0, 2])
def test_build_http_app_requires_single_mcp_route(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, route_count: int
) -> None:
    """_build_http_app fails loudly unless the SDK app has exactly one MCP route to replace."""
    module = _load_http(load_server)
    routes = [Route(CLAUDE_CODE_PATH, endpoint=module.app) for _ in range(route_count)]
    monkeypatch.setattr(module.mcp, "streamable_http_app", lambda: Starlette(routes=routes))
    with pytest.raises(RuntimeError, match="MCP route"):
        module._build_http_app()


def test_stdio_main_runs_stdio(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as a script in stdio mode, the server calls mcp.run over stdio."""
    launches = _record_launches(monkeypatch)
    load_server(run_as_main=True)
    assert launches == [("mcp.run", "stdio")]


def test_http_main_serves_app_with_uvicorn(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Run as a script in HTTP mode, the server serves `app` with uvicorn on MCP_HOST/MCP_PORT."""
    launches = _record_launches(monkeypatch)
    module = load_server(
        run_as_main=True, MCP_TRANSPORT="streamable-http", MCP_HOST="localhost", MCP_PORT="9123"
    )
    app = getattr(module, "app", None)
    assert launches == [("uvicorn.run", app, {"host": "localhost", "port": 9123})]


# Endpoint identity and MCP_PUBLIC_URL


def _add_probe_tools(module: ModuleType) -> None:
    """Register test-only tools that report `_current_endpoint()` in different situations."""
    entered: list[str] = []

    def whoami() -> str:
        """Return the current endpoint value."""
        return module._current_endpoint().value

    def whoami_then_zendesk() -> str:
        """Resolve the endpoint, then make one Zendesk call."""
        endpoint = module._current_endpoint()
        module._get("/tickets/1.json")
        return endpoint.value

    def whoami_in_worker() -> str:
        """Resolve at entry and carry it to a worker, and resolve again inside the worker."""
        carried = module._current_endpoint()
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                in_worker = executor.submit(module._current_endpoint).result().value
            except RuntimeError as error:
                in_worker = f"RuntimeError: {error}"
            carried_in_worker = executor.submit(lambda: carried).result().value
        return json.dumps({"in_worker": in_worker, "carried": carried_in_worker})

    async def whoami_when_both_in() -> str:
        """Wait until two calls are in flight, then return the current endpoint value."""
        entered.append("call")
        with anyio.fail_after(5):
            while len(entered) < 2:
                await anyio.sleep(0.01)
        return module._current_endpoint().value

    for tool in (whoami, whoami_then_zendesk, whoami_in_worker, whoami_when_both_in):
        module.mcp.add_tool(tool)


def _call(client: TestClient, path: str, name: str) -> tuple[bool, str]:
    """Call a no-argument tool over HTTP and return (isError, first text content)."""
    result = _rpc(client, path, "tools/call", {"name": name, "arguments": {}}).json()["result"]
    return result["isError"], result["content"][0]["text"]


async def _call_in_memory(module: ModuleType, name: str) -> tuple[bool, str]:
    """Call a tool through an in-memory (non-HTTP) session and return (isError, text)."""
    async with create_connected_server_and_client_session(module.mcp) as session:
        result = await session.call_tool(name, {})
    return result.isError, result.content[0].text


def _transport_security(module: ModuleType) -> Callable[[str], Any]:
    """Return the module's `_transport_security` builder, failing if it is missing."""
    builder = getattr(module, "_transport_security", None)
    assert callable(builder), "HTTP mode must define _transport_security"
    return builder


def _unwrap_endpoint_tag(module: ModuleType, endpoint: Any) -> Any:
    """Follow `.app` through any wrapping middleware to the _EndpointTag; return what it wraps."""
    while not isinstance(endpoint, module._EndpointTag):
        endpoint = endpoint.app
    return endpoint.app


def test_current_endpoint_matches_path(load_server: Callable[..., ModuleType]) -> None:
    """A real tools/call on each path reaches a tool body that sees that path's endpoint."""
    module = _load_http(load_server)
    _add_probe_tools(module)
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert _call(client, ZENDESK_FLOW_PATH, "whoami") == (False, "zendesk_flow")
        assert _call(client, CLAUDE_CODE_PATH, "whoami") == (False, "claude_code")


def test_current_endpoint_in_tools_list(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """_current_endpoint() also resolves inside a tools/list handler, per path."""
    module = _load_http(load_server)
    seen: list[str] = []
    list_tools = module.mcp._tool_manager.list_tools

    def spy_list_tools() -> list:
        """Record the endpoint, then list the tools as usual."""
        seen.append(module._current_endpoint().value)
        return list_tools()

    monkeypatch.setattr(module.mcp._tool_manager, "list_tools", spy_list_tools)
    with TestClient(module.app, base_url=BASE_URL) as client:
        for path in (ZENDESK_FLOW_PATH, CLAUDE_CODE_PATH):
            assert _rpc(client, path, "tools/list").status_code == 200
    assert seen == ["zendesk_flow", "claude_code"]


def test_current_endpoint_concurrent_paths(load_server: Callable[..., ModuleType]) -> None:
    """Two overlapping calls on different paths each see their own path's endpoint."""
    module = _load_http(load_server)
    _add_probe_tools(module)
    with TestClient(module.app, base_url=BASE_URL) as client, ThreadPoolExecutor(2) as executor:
        flow = executor.submit(_call, client, ZENDESK_FLOW_PATH, "whoami_when_both_in")
        claude = executor.submit(_call, client, CLAUDE_CODE_PATH, "whoami_when_both_in")
        assert flow.result() == (False, "zendesk_flow")
        assert claude.result() == (False, "claude_code")


@pytest.mark.parametrize("case", ["untagged_route", "string_tag", "stdio_tag", "outside_request"])
def test_current_endpoint_refuses_unknown_route(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """In HTTP mode a call without a known Endpoint tag is refused before any Zendesk call."""
    module = _load_http(load_server)
    _add_probe_tools(module)
    seen = _stub_zendesk(module, monkeypatch)
    if case == "outside_request":
        is_error, text = anyio.run(_call_in_memory, module, "whoami_then_zendesk")
    else:
        flow_route = next(
            route for route in module.app.router.routes if route.path == ZENDESK_FLOW_PATH
        )
        transport_endpoint = _unwrap_endpoint_tag(module, flow_route.endpoint)
        tags = {"string_tag": "claude_code", "stdio_tag": module.Endpoint.STDIO}
        endpoint = transport_endpoint
        if case in tags:
            endpoint = module._EndpointTag(transport_endpoint, tags[case])
        module.app.router.routes.append(Route("/other/mcp", endpoint=endpoint))
        with TestClient(module.app, base_url=BASE_URL) as client:
            is_error, text = _call(client, "/other/mcp", "whoami_then_zendesk")
    assert is_error is True
    assert "Cannot tell which endpoint" in text
    assert seen == []
    if case == "outside_request":
        with pytest.raises(RuntimeError, match="Cannot tell which endpoint"):
            module._current_endpoint()


def test_current_endpoint_http_worker_thread(load_server: Callable[..., ModuleType]) -> None:
    """In HTTP mode a worker thread cannot resolve the endpoint; a value resolved at entry carries."""
    module = _load_http(load_server)
    _add_probe_tools(module)
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _call(client, ZENDESK_FLOW_PATH, "whoami_in_worker")
    assert is_error is False, text
    body = json.loads(text)
    assert body["carried"] == "zendesk_flow"
    assert body["in_worker"].startswith("RuntimeError: Cannot tell which endpoint")


def test_current_endpoint_stdio(load_server: Callable[..., ModuleType]) -> None:
    """Stdio mode returns STDIO in a session, in a worker thread, and outside a request."""
    module = load_server()
    _add_probe_tools(module)
    assert anyio.run(_call_in_memory, module, "whoami") == (False, "stdio")
    is_error, text = anyio.run(_call_in_memory, module, "whoami_in_worker")
    assert is_error is False, text
    assert json.loads(text) == {"in_worker": "stdio", "carried": "stdio"}
    assert module._current_endpoint() is module.Endpoint.STDIO


@pytest.mark.parametrize(
    "url,hosts,origins",
    [
        (
            "https://mcp.example.com",
            ["mcp.example.com", "mcp.example.com:443"],
            ["https://mcp.example.com", "https://mcp.example.com:443"],
        ),
        (
            "https://MCP.Example.com:443",
            ["mcp.example.com", "mcp.example.com:443"],
            ["https://mcp.example.com", "https://mcp.example.com:443"],
        ),
        (
            "https://mcp.example.com:8443",
            ["mcp.example.com:8443"],
            ["https://mcp.example.com:8443"],
        ),
        ("https://[2001:db8::1]:8443", ["[2001:db8::1]:8443"], ["https://[2001:db8::1]:8443"]),
    ],
)
def test_transport_security_public_entries(
    load_server: Callable[..., ModuleType], url: str, hosts: list[str], origins: list[str]
) -> None:
    """The public URL adds its hosts and origins after the loopback entries, protection on."""
    security = _transport_security(_load_http(load_server))(url)
    assert security.enable_dns_rebinding_protection is True
    assert security.allowed_hosts == ["127.0.0.1:*", "localhost:*", "[::1]:*", *hosts]
    assert security.allowed_origins[3:] == origins


@pytest.mark.parametrize(
    "url",
    [
        "mcp.example.com",
        "ftp://mcp.example.com",
        "https://",
        "https://user:pw@mcp.example.com",
        "https://user@mcp.example.com",
        "https://:pw@mcp.example.com",
        "https://mcp.example.com/zendesk/mcp",
        "https://mcp.example.com?x=1",
        "https://mcp.example.com#frag",
        "https://mcp.example.com:abc",
        "https://mcp.example.com:0",
        "https://mcp.example.com ",
        "https://[::1",
        "http://mcp.example.com",
        "https://mcp.example.com.",
        "https://mcp.example.com;x",
        "https://mcp.example.com/;x",
        "https://mcp.exa\tmple.com",
        "https://mcp.exa mple.com",
        "https://mcp\x01.example.com",
        "https://mcp_x.example.com",
        "https://mcp.ex%41mple.com",
        "https://@mcp.example.com",
        "https://mcp.example.com/",
        "https://[fe80::1%25eth0]",
        "https://[v1.x]",
    ],
)
def test_http_mode_rejects_bad_public_url(load_server: Callable[..., ModuleType], url: str) -> None:
    """A malformed MCP_PUBLIC_URL raises ValueError at import, naming it invalid."""
    with pytest.raises(Exception, match="MCP_PUBLIC_URL") as caught:
        load_server(MCP_TRANSPORT="streamable-http", MCP_PUBLIC_URL=url)
    assert caught.type is ValueError, caught.value
    assert "Invalid MCP_PUBLIC_URL" in str(caught.value)


def test_public_url_empty_means_unset(load_server: Callable[..., ModuleType]) -> None:
    """An empty MCP_PUBLIC_URL is treated as unset: the server loads with loopback entries only."""
    module = _load_http(load_server, MCP_PUBLIC_URL="")
    loopback_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert module.mcp.settings.transport_security.allowed_hosts == loopback_hosts


def test_stdio_ignores_public_url(load_server: Callable[..., ModuleType]) -> None:
    """Stdio does not read MCP_PUBLIC_URL, so any value imports."""
    for value in ("not a url", "https://mcp.example.com"):
        assert load_server(MCP_PUBLIC_URL=value).TRANSPORT == "stdio"


def test_non_loopback_host_needs_public_url(load_server: Callable[..., ModuleType]) -> None:
    """A non-loopback MCP_HOST is refused without MCP_PUBLIC_URL and accepted with it."""
    with pytest.raises(RuntimeError, match="MCP_PUBLIC_URL"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_HOST="0.0.0.0")
    module = _load_http(load_server, MCP_HOST="0.0.0.0", MCP_PUBLIC_URL="https://mcp.example.com")
    assert _module_attr(module, "RESOURCE_BASE_URL") == "https://mcp.example.com"


def test_public_url_keeps_port_check(load_server: Callable[..., ModuleType]) -> None:
    """The MCP_PORT check runs before MCP_PUBLIC_URL is validated."""
    with pytest.raises(ValueError, match="Invalid MCP_PORT"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_PORT="0", MCP_PUBLIC_URL="not a url")


def test_public_url_real_startup(load_server: Callable[..., ModuleType]) -> None:
    """Started with a public URL on 0.0.0.0, the server needs a token and checks Host and Origin."""
    module = _load_http(load_server, MCP_HOST="0.0.0.0", MCP_PUBLIC_URL="https://mcp.example.com")
    with TestClient(module.app, base_url="https://mcp.example.com") as client:

        def status(path: str = CLAUDE_CODE_PATH, **headers: str) -> int:
            """Return the HTTP status of a tools/list request with the given headers."""
            return _rpc(client, path, "tools/list", **headers).status_code

        anonymous = client.post(CLAUDE_CODE_PATH, json={}, headers=NO_AUTH_HEADERS)
        assert anonymous.status_code == 401
        assert anonymous.headers["www-authenticate"].endswith(
            'resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource'
            '/claude/mcp"'
        )
        assert status(host="mcp.example.com") == 200
        assert status(ZENDESK_FLOW_PATH, host="mcp.example.com") == 200
        assert status(host="mcp.example.com:443") == 200
        assert status(host="mcp.example.com:8443") == 421
        assert status(host="evil.com") == 421
        assert status(host="mcp.example.com", origin="https://mcp.example.com") == 200
        assert status(host="mcp.example.com", origin="https://evil.com") == 403
        assert status(host="mcp.example.com", origin="http://mcp.example.com") == 403


# Caller authentication


def _token_check_stub(
    module: ModuleType, monkeypatch: pytest.MonkeyPatch, answer: Callable[[], httpx.Response]
) -> list[httpx.Request]:
    """Stub Zendesk so `tokens/current` returns `answer()`; return the token checks made."""
    checks: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the token check and return the canned answer."""
        checks.append(request)
        return answer()

    stub = httpx.Client(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "_client", stub)
    return checks


@pytest.mark.parametrize("path", [ZENDESK_FLOW_PATH, CLAUDE_CODE_PATH])
@pytest.mark.parametrize("authorization", [None, "Bearer wrong-token", "Basic dXNlcjpwdw=="])
def test_http_endpoints_require_token(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    authorization: str | None,
) -> None:
    """Both endpoints answer 401 with a Bearer challenge and make no Zendesk API call."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    headers = dict(NO_AUTH_HEADERS)
    if authorization is not None:
        headers["authorization"] = authorization
    with TestClient(module.app, base_url=BASE_URL) as client:
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": GET_TICKET_CALL}
        response = client.post(path, json=body, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith('Bearer error="invalid_token"')
    assert seen == []


def test_http_token_without_read_scope_is_403(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token that lacks `read` gets 403 insufficient_scope."""
    module = _load_http(load_server)
    info = {**TOKEN_INFO, "scopes": ["tickets:read"]}
    _token_check_stub(module, monkeypatch, lambda: httpx.Response(200, json={"token": info}))
    with TestClient(module.app, base_url=BASE_URL) as client:
        response = _rpc(client, CLAUDE_CODE_PATH, "tools/list")
    assert response.status_code == 403
    assert 'error="insufficient_scope"' in response.headers["www-authenticate"]


def _raise_connect_error() -> httpx.Response:
    """Stand-in for an unreachable Zendesk."""
    raise httpx.ConnectError("connection refused")


@pytest.mark.parametrize(
    "answer",
    [
        lambda: httpx.Response(429, headers={"retry-after": "30"}),
        lambda: httpx.Response(502),
        lambda: httpx.Response(200, json={"unexpected": True}),
        _raise_connect_error,
    ],
    ids=["429", "502", "bad_body", "network"],
)
def test_http_token_check_failure_is_503(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    answer: Callable[[], httpx.Response],
) -> None:
    """When Zendesk cannot check the token, the client gets 503, not 401, and no token text."""
    module = _load_http(load_server)
    _token_check_stub(module, monkeypatch, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        response = _rpc(client, CLAUDE_CODE_PATH, "tools/list")
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert response.json()["error"] == "temporarily_unavailable"
    assert CALLER_TOKEN not in response.text


def test_verifier_caches_valid_tokens_only(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token is checked once per cache period; a rejected token is checked every time."""
    module = _load_http(load_server)
    checks: list[httpx.Request] = []
    monkeypatch.setattr(module, "_client", _zendesk_stub_client(module, [], checks))
    verifier = _module_attr(module, "ZendeskTokenVerifier")()
    clock = [1_000_000.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])

    first = anyio.run(verifier.verify_token, CALLER_TOKEN)
    assert anyio.run(verifier.verify_token, CALLER_TOKEN) == first
    assert (first.user_id, first.scopes, len(checks)) == (42, ["read"], 1)
    clock[0] += module._TOKEN_CACHE_SECONDS + 1
    anyio.run(verifier.verify_token, CALLER_TOKEN)
    assert len(checks) == 2
    assert anyio.run(verifier.verify_token, "bad") is None
    assert anyio.run(verifier.verify_token, "bad") is None
    assert len(checks) == 4
    assert all(CALLER_TOKEN not in key for key in verifier._cache)


def test_verifier_cache_stops_at_expires_at(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A token expiring in 10 s is cached for 10 s, not the full cache period."""
    module = _load_http(load_server)
    info = {**TOKEN_INFO, "expires_at": "2026-11-11T20:00:10Z"}
    checks = _token_check_stub(
        module, monkeypatch, lambda: httpx.Response(200, json={"token": info})
    )
    expires_epoch = _module_attr(module, "_parse_expires_at")("2026-11-11T20:00:10Z")
    clock = [expires_epoch - 10.0]
    monkeypatch.setattr(module.time, "time", lambda: clock[0])
    verifier = module.ZendeskTokenVerifier()
    assert anyio.run(verifier.verify_token, CALLER_TOKEN).expires_at == expires_epoch
    clock[0] = expires_epoch + 1
    anyio.run(verifier.verify_token, CALLER_TOKEN)
    assert len(checks) == 2


def test_verifier_cache_is_bounded(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache never holds more than its maximum number of entries."""
    module = _load_http(load_server)
    _token_check_stub(module, monkeypatch, lambda: httpx.Response(200, json={"token": TOKEN_INFO}))
    verifier = _module_attr(module, "ZendeskTokenVerifier")()
    monkeypatch.setattr(module, "_TOKEN_CACHE_MAX_ENTRIES", 3)
    for number in range(5):
        anyio.run(verifier.verify_token, f"token-{number}")
    assert len(verifier._cache) == 3


@pytest.mark.parametrize(
    "value,expected",
    [("2026-11-11T20:52:43Z", 1794430363), (None, None), ("tomorrow", None), (5, None)],
)
def test_parse_expires_at(
    load_server: Callable[..., ModuleType], value: object, expected: int | None
) -> None:
    """Zendesk's UTC `Z` timestamps parse on every supported Python; anything else is None."""
    assert _module_attr(_load_http(load_server), "_parse_expires_at")(value) == expected


def test_http_token_never_in_reprs_or_logs(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Reprs of the auth objects and every log record of a full call omit the token."""
    module = _load_http(load_server)
    _stub_zendesk(module, monkeypatch)
    caplog.set_level("DEBUG")
    with TestClient(module.app, base_url=BASE_URL) as client:
        _rpc(client, CLAUDE_CODE_PATH, "tools/call", GET_TICKET_CALL)
    verifier = _module_attr(module, "ZendeskTokenVerifier")()
    token = anyio.run(verifier.verify_token, CALLER_TOKEN)
    assert token.token == CALLER_TOKEN
    texts = [
        repr(module._BearerAuth(CALLER_TOKEN)),
        repr(token),
        str(token),
        str(list(token.__rich_repr__())),
        str(token.model_dump()),
        token.model_dump_json(),
        caplog.text,
    ]
    assert all(CALLER_TOKEN not in text for text in texts)


def test_http_zendesk_auth_fails_closed_without_caller(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Outside a request, in a non-HTTP session, and in a worker, HTTP mode sends nothing."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)

    def ticket_in_worker() -> str:
        """Call `_get` from a worker thread inside a real request."""
        with ThreadPoolExecutor(max_workers=1) as executor:
            try:
                executor.submit(module._get, "/tickets/1.json").result()
            except RuntimeError as error:
                return str(error)
        return "sent"

    module.mcp.add_tool(ticket_in_worker)
    with pytest.raises(RuntimeError, match="No authenticated caller"):
        module._zendesk_auth()
    is_error, text = anyio.run(_call_in_memory_args, module, "get_ticket", {"ticket_id": 1})
    assert is_error and "No authenticated caller" in text
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert _call(client, CLAUDE_CODE_PATH, "ticket_in_worker")[1].startswith("No authenticated")
    assert seen == []


async def _call_in_memory_args(module: ModuleType, name: str, args: dict) -> tuple[bool, str]:
    """Call a tool with arguments through an in-memory (non-HTTP) session."""
    async with create_connected_server_and_client_session(module.mcp) as session:
        result = await session.call_tool(name, args)
    return result.isError, result.content[0].text


def test_http_bulk_comments_send_caller_token(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_comments_for_tickets workers send the caller's bearer token resolved at entry."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    call = {"name": "get_comments_for_tickets", "arguments": {"ticket_ids": [1, 2, 3]}}
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert (
            _rpc(client, ZENDESK_FLOW_PATH, "tools/call", call).json()["result"]["isError"] is False
        )
    assert {request.headers["authorization"] for request in seen} == {f"Bearer {CALLER_TOKEN}"}
    assert len(seen) == 3


def test_http_fetch_attachment_sends_bearer_and_keeps_guards(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch_attachment sends the caller's token, refuses off-subdomain URLs, follows no redirect."""
    module = _load_http(load_server)
    calls: list[dict] = []

    def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        """Record the call and return a redirect, built with the auth applied."""
        auth = kwargs["auth"]
        if isinstance(auth, tuple):  # httpx sends a (user, password) tuple as basic auth
            auth = httpx.BasicAuth(*auth)
        request = next(auth.auth_flow(httpx.Request(method, url)))
        calls.append({"authorization": request.headers["authorization"], **kwargs})
        response = httpx.Response(302, headers={"location": "https://cdn.example.net/f"})
        response.request = request
        return contextlib.nullcontext(response)

    monkeypatch.setattr(module.httpx, "stream", fake_stream)
    attachment = {"name": "fetch_attachment", "arguments": {"url": "https://example.zendesk.com/a"}}
    evil = {"name": "fetch_attachment", "arguments": {"url": "https://evil.com/a"}}
    with TestClient(module.app, base_url=BASE_URL) as client:
        redirect = _rpc(client, CLAUDE_CODE_PATH, "tools/call", attachment).json()["result"]
        refused = _rpc(client, CLAUDE_CODE_PATH, "tools/call", evil).json()["result"]
    assert json.loads(redirect["content"][0]["text"])["kind"] == "redirect"
    assert "error" in json.loads(refused["content"][0]["text"])
    assert len(calls) == 1
    assert calls[0]["authorization"] == f"Bearer {CALLER_TOKEN}"
    assert calls[0]["follow_redirects"] is False


# OAuth client pin, discovery, authorize redirect and write scope


@pytest.mark.parametrize(
    "value,error",
    [(None, KeyError), ("zendesk_mcp", ValueError), ("²", ValueError), ("07", ValueError)],
    ids=["missing", "name", "non_ascii_digit", "leading_zero"],
)
def test_http_mode_needs_numeric_oauth_client_id(
    load_server: Callable[..., ModuleType], value: str | None, error: type[Exception]
) -> None:
    """HTTP mode fails at import without a numeric ZENDESK_OAUTH_CLIENT_ID; stdio does not need it."""
    with pytest.raises(error, match="ZENDESK_OAUTH_CLIENT_ID"):
        load_server(MCP_TRANSPORT="streamable-http", ZENDESK_OAUTH_CLIENT_ID=value)
    assert load_server(MCP_TRANSPORT=None, ZENDESK_OAUTH_CLIENT_ID=value).TRANSPORT == "stdio"


def test_verifier_rejects_other_oauth_client(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid token issued to another OAuth client on the instance is treated as invalid."""
    module = _load_http(load_server)
    info = {**TOKEN_INFO, "client_id": 8}
    _token_check_stub(module, monkeypatch, lambda: httpx.Response(200, json={"token": info}))
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert _rpc(client, CLAUDE_CODE_PATH, "tools/list").status_code == 401
    verifier = _module_attr(module, "ZendeskTokenVerifier")()
    assert anyio.run(verifier.verify_token, CALLER_TOKEN) is None


@pytest.mark.parametrize(
    "env,base",
    [
        ({}, "http://127.0.0.1:8000"),
        ({"MCP_HOST": "::1", "MCP_PORT": "9000"}, "http://[::1]:9000"),
        ({"MCP_PUBLIC_URL": "https://MCP.example.com:443"}, "https://mcp.example.com"),
        ({"MCP_PUBLIC_URL": "https://mcp.example.com:8443"}, "https://mcp.example.com:8443"),
    ],
)
def test_resource_base_url(
    load_server: Callable[..., ModuleType], env: dict[str, str], base: str
) -> None:
    """The resource base is the public URL, or the loopback host and port when it is unset."""
    assert _module_attr(_load_http(load_server, **env), "RESOURCE_BASE_URL") == base


def test_discovery_documents(load_server: Callable[..., ModuleType]) -> None:
    """Each path's 401 names its own metadata; PRM and AS metadata have the agreed values."""
    module = _load_http(load_server)
    well_known = f"{BASE_URL}/.well-known/oauth-protected-resource"
    with TestClient(module.app, base_url=BASE_URL) as client:
        for path, scopes in ((ZENDESK_FLOW_PATH, ["read"]), (CLAUDE_CODE_PATH, ["read", "write"])):
            challenge = client.post(path, json={}, headers=NO_AUTH_HEADERS)
            assert challenge.status_code == 401, path
            assert (
                f'resource_metadata="{well_known}{path}"' in challenge.headers["www-authenticate"]
            )
            document = client.get(f"/.well-known/oauth-protected-resource{path}").json()
            assert document["resource"] == BASE_URL + path
            assert document["authorization_servers"] == [BASE_URL + "/"]
            assert document["scopes_supported"] == scopes
        root = client.get("/.well-known/oauth-protected-resource").json()
        server = client.get("/.well-known/oauth-authorization-server").json()
    assert (root["resource"], root["scopes_supported"]) == (BASE_URL + "/", ["read"])
    assert server["issuer"] == BASE_URL + "/"
    assert server["authorization_endpoint"] == BASE_URL + "/oauth/authorize"
    assert server["token_endpoint"] == "https://example.zendesk.com/oauth/tokens"
    assert server["code_challenge_methods_supported"] == ["S256"]
    assert server["token_endpoint_auth_methods_supported"] == ["client_secret_post"]
    assert "registration_endpoint" not in server


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            (
                "response_type=code&client_id=zendesk_mcp&scope=read%20write&state=a%2Bb~c"
                "&resource=https%3A%2F%2Fh%2Fclaude%2Fmcp&redirect_uri=http%3A%2F%2Flocalhost"
                "%3A8765%2Fcallback"
            ),
            (
                "response_type=code&client_id=zendesk_mcp&scope=read%20write&state=a%2Bb~c"
                "&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback"
            ),
        ),
        ("resourc%65=x&resource=y&client_id=c", "client_id=c"),
        ("resource=only", ""),
        ("", ""),
    ],
    ids=["typical", "encoded_name", "only_resource", "empty"],
)
def test_authorize_redirect_drops_only_resource(
    load_server: Callable[..., ModuleType], query: str, expected: str
) -> None:
    """GET /oauth/authorize 302s to the configured Zendesk host, minus every `resource`."""
    module = _load_http(load_server)
    target = "https://example.zendesk.com/oauth/authorizations/new"
    with TestClient(module.app, base_url=BASE_URL) as client:
        response = client.get(f"/oauth/authorize?{query}", follow_redirects=False)
        assert client.post("/oauth/authorize").status_code == 405
    assert response.status_code == 302
    assert response.headers["location"] == (f"{target}?{expected}" if expected else target)


@pytest.mark.parametrize(
    "scopes,allowed",
    [(["read"], False), (["read", "tickets:write"], False), (["read", "write"], True)],
)
def test_http_write_tool_needs_write_scope(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    scopes: list[str],
    allowed: bool,
) -> None:
    """A write tool refuses a read-only token before any Zendesk call, and runs with `write`."""
    module = _load_http(load_server, ACCESS_LEVEL="management")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer token checks with the given scopes and record other requests."""
        if request.url.path.endswith("/oauth/tokens/current.json"):
            return httpx.Response(200, json={"token": {**TOKEN_INFO, "scopes": scopes}})
        seen.append(request)
        return httpx.Response(200, json={"ticket": {"id": 1}})

    stub = httpx.Client(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "_client", stub)
    call = {"name": "add_ticket_tags", "arguments": {"ticket_id": 1, "tags": ["x"]}}
    with TestClient(module.app, base_url=BASE_URL) as client:
        result = _rpc(client, CLAUDE_CODE_PATH, "tools/call", call).json()["result"]
    assert result["isError"] is not allowed
    assert len(seen) == (2 if allowed else 0)  # add_ticket_tags reads the ticket, then updates it
    for request in seen:
        assert request.headers["authorization"] == f"Bearer {CALLER_TOKEN}"
    if not allowed:
        assert "no `write` scope" in result["content"][0]["text"]


def _write_tool_definitions() -> dict[str, ast.FunctionDef]:
    """Return every public function in server.py that sends POST, PUT or DELETE through a helper."""
    write_helpers = {"_post", "_put", "_delete", "_post_upload"}
    definitions = {}
    for node in ast.walk(ast.parse(SERVER_FILE.read_text())):
        if not isinstance(node, ast.FunctionDef) or node.name.startswith("_"):
            continue
        called = {
            call.func.id
            for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }
        if called & write_helpers:
            definitions[node.name] = node
    return definitions


def test_write_tools_check_scope_first(load_server: Callable[..., ModuleType]) -> None:
    """Every write tool calls _require_write_scope() as its first statement after the docstring."""
    definitions = _write_tool_definitions()
    http_tools = set(
        _tool_names(load_server(MCP_TRANSPORT="streamable-http", ACCESS_LEVEL="admin"))
    )
    assert len(definitions) == 13
    assert len(http_tools & set(definitions)) == 12
    for name, node in definitions.items():
        first = node.body[1]  # body[0] is the docstring
        assert isinstance(first, ast.Expr) and isinstance(first.value, ast.Call), name
        assert ast.unparse(first.value) == "_require_write_scope()", name


def test_write_tool_descriptions_name_write_scope(load_server: Callable[..., ModuleType]) -> None:
    """Over HTTP every write tool's description says it needs `write`; stdio text is untouched."""
    write_tools = set(_write_tool_definitions())
    http = load_server(MCP_TRANSPORT="streamable-http", ACCESS_LEVEL="admin")
    for tool in anyio.run(http.mcp.list_tools):
        has_note = HTTP_WRITE_SCOPE_NOTE in " ".join(tool.description.split())
        assert has_note is (tool.name in write_tools), tool.name
    assert _module_attr(http, "_HTTP_WRITE_SCOPE_NOTE") == HTTP_WRITE_SCOPE_NOTE
    stdio = load_server(MCP_TRANSPORT=None, ACCESS_LEVEL="admin")
    for tool in anyio.run(stdio.mcp.list_tools):
        assert "`write` scope" not in tool.description, tool.name
        assert tool.description == getattr(stdio, tool.name).__doc__, tool.name


def test_http_client_never_replays_cookies(load_server: Callable[..., ModuleType]) -> None:
    """The real shared client stores no Zendesk cookie, so no caller's cookie reaches another."""
    module = load_server(MCP_TRANSPORT="streamable-http")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Set a session cookie on every answer and record each request."""
        seen.append(request)
        cookie = {"set-cookie": "_zendesk_session=SESSION; Path=/; Secure"}
        if request.url.path.endswith("/oauth/tokens/current.json"):
            return httpx.Response(200, headers=cookie, json={"token": TOKEN_INFO})
        return httpx.Response(200, headers=cookie, json={"ticket": {"id": 1}})

    module._client._transport = httpx.MockTransport(handler)  # keep the real client and its jar
    with TestClient(module.app, base_url=BASE_URL) as client:
        for _ in range(2):
            _rpc(client, CLAUDE_CODE_PATH, "tools/call", GET_TICKET_CALL)
    assert [request.headers.get("cookie") for request in seen] == [None] * len(seen)
    assert len(module._client.cookies.jar) == 0
    assert len(seen) >= 3  # two tool calls plus at least one token check


@pytest.mark.parametrize("status", [204, 302, 401, 403, 404])
def test_verifier_rejects_non_200(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    """Any answer other than 200, 429 or 5xx means the token is not valid: the caller gets 401."""
    module = _load_http(load_server)
    _token_check_stub(module, monkeypatch, lambda: httpx.Response(status))
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert _rpc(client, CLAUDE_CODE_PATH, "tools/list").status_code == 401
    verifier = _module_attr(module, "ZendeskTokenVerifier")()
    assert anyio.run(verifier.verify_token, CALLER_TOKEN) is None


def test_verifier_sends_bearer_to_tokens_current(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One GET to tokens/current with the token as bearer auth and the 10 s timeout."""
    module = _load_http(load_server)
    checks = _token_check_stub(
        module, monkeypatch, lambda: httpx.Response(200, json={"token": TOKEN_INFO})
    )
    anyio.run(_module_attr(module, "ZendeskTokenVerifier")().verify_token, CALLER_TOKEN)
    assert len(checks) == 1
    assert (checks[0].method, checks[0].url.path) == ("GET", "/api/v2/oauth/tokens/current.json")
    assert checks[0].headers["authorization"] == f"Bearer {CALLER_TOKEN}"
    assert checks[0].extensions["timeout"] == httpx.Timeout(10.0).as_dict()


def test_http_two_callers_each_send_own_token(load_server: Callable[..., ModuleType]) -> None:
    """Interleaved calls from two callers, thread pool included: each Zendesk request carries its
    own caller's bearer token and no cookie. Uses the real shared client with a stub transport."""
    module = load_server(MCP_TRANSPORT="streamable-http")
    tokens = {"A": "caller-a-token-0123456789", "B": "caller-b-token-0123456789"}
    ticket_owner = {**dict.fromkeys((1, 2, 3), "A"), **dict.fromkeys((11, 12, 13), "B")}
    sent: list[tuple[int, str | None, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Accept both tokens; record ticket requests; set a cookie on every answer."""
        cookie = {"set-cookie": "_zendesk_session=SESSION; Path=/; Secure"}
        if request.url.path.endswith("/oauth/tokens/current.json"):
            return httpx.Response(200, headers=cookie, json={"token": TOKEN_INFO})
        ticket_id = int(re.search(r"/tickets/(\d+)", request.url.path).group(1))
        sent.append(
            (ticket_id, request.headers.get("authorization"), request.headers.get("cookie"))
        )
        body = {"ticket": {"id": ticket_id}, "comments": [], "next_page": None}
        return httpx.Response(200, headers=cookie, json=body)

    module._client._transport = httpx.MockTransport(handler)  # keep the real client and its jar
    calls = [
        ("A", "get_ticket", {"ticket_id": 1}),
        ("B", "get_comments_for_tickets", {"ticket_ids": [11, 12, 13]}),
        ("A", "get_comments_for_tickets", {"ticket_ids": [2, 3]}),
        ("B", "get_ticket", {"ticket_id": 11}),
    ]
    with TestClient(module.app, base_url=BASE_URL) as client, ThreadPoolExecutor(4) as executor:
        futures = [
            executor.submit(
                _rpc,
                client,
                CLAUDE_CODE_PATH,
                "tools/call",
                {"name": name, "arguments": arguments},
                authorization=f"Bearer {tokens[caller]}",
            )
            for caller, name, arguments in calls
        ]
        results = [future.result().json()["result"] for future in futures]
    assert all(result["isError"] is False for result in results)
    assert len(sent) == 7
    for ticket_id, authorization, cookie in sent:
        assert authorization == f"Bearer {tokens[ticket_owner[ticket_id]]}", ticket_id
        assert cookie is None, ticket_id


# Time budget on /zendesk/mcp


class _FakeClock:
    """A thread-safe monotonic clock that only moves when told to; records sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self._lock = threading.Lock()

    def monotonic(self) -> float:
        """Return the current fake time."""
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        """Move the clock forward."""
        with self._lock:
            self.now += seconds

    def sleep(self, seconds: float) -> None:
        """Record a sleep and advance by it, without waiting."""
        self.sleeps.append(seconds)
        self.advance(seconds)


def _slow_zendesk(
    module: ModuleType,
    clock: _FakeClock,
    answer: Callable[[httpx.Request], tuple[float, httpx.Response]],
    token_check_seconds: float = 0.0,
    scopes: list[str] | None = None,
) -> list[tuple[float, httpx.Request]]:
    """Stub Zendesk on the fake clock: each answer takes its latency, or times out at the read cap.

    Returns the (start time, request) list of non-token requests.
    """
    sent: list[tuple[float, httpx.Request]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Answer token checks after `token_check_seconds`; others per `answer`."""
        if request.url.path.endswith("/oauth/tokens/current.json"):
            clock.advance(token_check_seconds)
            info = {**TOKEN_INFO, "scopes": scopes or ["read"]}
            return httpx.Response(200, json={"token": info})
        sent.append((clock.monotonic(), request))
        latency, response = answer(request)
        read_cap = request.extensions["timeout"]["read"]
        if read_cap is not None and latency > read_cap:
            clock.advance(read_cap)
            raise httpx.ReadTimeout("slow", request=request)
        clock.advance(latency)
        return response

    module._client = httpx.Client(
        base_url=module.BASE_URL, timeout=30.0, transport=httpx.MockTransport(handler)
    )
    return sent


def _with_clock(module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Replace the module's `time` with a fake clock (monotonic, sleep); `time()` stays real.

    Only this module copy sees it, and it works on origin/main too, so red runs fail on
    assertions rather than on a missing hook.
    """
    clock = _FakeClock()
    fake_time = types.SimpleNamespace(monotonic=clock.monotonic, sleep=clock.sleep, time=time.time)
    monkeypatch.setattr(module, "time", fake_time)
    return clock


def _export_page(request: httpx.Request) -> tuple[float, httpx.Response]:
    """An export page that takes 10 s and always has more."""
    page = {"results": [{"id": 1}], "meta": {"has_more": True, "after_cursor": "c"}}
    return 10.0, httpx.Response(200, json=page)


def _tool_text(client: TestClient, path: str, name: str, arguments: dict) -> tuple[bool, str]:
    """Call a tool over HTTP and return (isError, first text content)."""
    call = {"name": name, "arguments": arguments}
    result = _rpc(client, path, "tools/call", call).json()["result"]
    return result["isError"], result["content"][0]["text"]


def _json_object(text: str) -> dict:
    """Parse a tool answer that must be a JSON object; fail on an assertion otherwise."""
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        body = None
    assert isinstance(body, dict), text[:300]
    return body


def test_flow_export_stops_at_budget(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On /zendesk/mcp the export stops by the budget with partial results and a warning."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _export_page)
    with TestClient(module.app, base_url=BASE_URL) as client:
        start = clock.monotonic()
        is_error, text = _tool_text(
            client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"}
        )
    body = json.loads(text)
    assert is_error is False
    assert body["returned_count"] == 2
    assert "25 s time budget" in body["warning"]
    assert clock.monotonic() - start <= 25.0
    assert [request.extensions["timeout"]["read"] for _, request in sent] == [25.0, 15.0, 5.0]


def test_flow_deadline_counts_token_check(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The clock starts before the token check: an 8 s check leaves 17 s for the tool."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _export_page, token_check_seconds=8.0)
    with TestClient(module.app, base_url=BASE_URL) as client:
        start = clock.monotonic()
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    assert json.loads(text)["returned_count"] == 1
    assert clock.monotonic() - start <= 25.0
    assert sent[0][1].extensions["timeout"]["read"] == 17.0


@pytest.mark.parametrize("path", [CLAUDE_CODE_PATH, "stdio"])
def test_no_deadline_off_flow_path(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """On /claude/mcp and stdio the same slow export runs to its own caps, client timeout intact."""
    module = load_server() if path == "stdio" else _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = itertools.count(1)

    def five_pages(request: httpx.Request) -> tuple[float, httpx.Response]:
        """10 s pages; the fifth is the last."""
        more = next(pages) < 5
        page = {"results": [{"id": 1}], "meta": {"has_more": more, "after_cursor": "c"}}
        return 10.0, httpx.Response(200, json=page)

    if path == "stdio":
        sent = _slow_zendesk(module, clock, five_pages)
        is_error, text = anyio.run(
            _call_in_memory_args, module, "export_search_results", {"query": "x"}
        )
    else:
        sent = _slow_zendesk(module, clock, five_pages)
        with TestClient(module.app, base_url=BASE_URL) as client:
            is_error, text = _tool_text(client, path, "export_search_results", {"query": "x"})
    body = json.loads(text)
    assert is_error is False
    assert body["returned_count"] == 5 and "warning" not in body
    assert {str(request.extensions["timeout"]) for _, request in sent} == {
        str(httpx.Timeout(30.0).as_dict())
    }
    assert sent[0][1].url.params["page[size]"] == "1000"  # flow pages are 100


def test_flow_retry_after_longer_than_time_left(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Retry-After that would overrun the budget returns partial results at once, no sleep."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = itertools.count(1)

    def then_429(request: httpx.Request) -> tuple[float, httpx.Response]:
        """First page fine; then a 429 asking for 30 s."""
        if next(pages) == 1:
            return _export_page(request)
        return 1.0, httpx.Response(429, headers={"retry-after": "30"})

    _slow_zendesk(module, clock, then_429)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    assert _json_object(text)["returned_count"] == 1
    assert clock.sleeps == []


def test_flow_bulk_comments_workers_get_deadline(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers carry the deadline: no request starts within 1 s of it; unfetched tickets listed."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)

    def slow_thread(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Each comment page takes 2 s."""
        return 2.0, httpx.Response(200, json={"comments": [], "next_page": None})

    sent = _slow_zendesk(module, clock, slow_thread)
    call = {"ticket_ids": list(range(1, 41)), "max_concurrency": 8}
    with TestClient(module.app, base_url=BASE_URL) as client:
        start = clock.monotonic()
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_comments_for_tickets", call)
    body = json.loads(text)
    assert is_error is False
    deadline = start + 25.0
    assert all(started <= deadline - 1.0 for started, _ in sent)
    assert len(body["comments_by_ticket"]) == 40
    unfinished = body["unfinished"]
    assert 0 < len(unfinished) < 40 and "errors" not in body
    assert unfinished["40"] == "/tickets/40/comments.json"
    assert "get_ticket_comments" in body["warning"]


def test_flow_write_reachable_and_never_clamped(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write-scoped token can write via /zendesk/mcp; the PUT keeps the client timeout, and a
    write is not started with under 5 s left."""
    module = _load_http(load_server, ACCESS_LEVEL="management")
    clock = _with_clock(module, monkeypatch)
    get_latency = {"value": 1.0}

    def ticket(request: httpx.Request) -> tuple[float, httpx.Response]:
        """GET takes `get_latency`; PUT 1 s."""
        latency = get_latency["value"] if request.method == "GET" else 1.0
        return latency, httpx.Response(200, json={"ticket": {"id": 1, "tags": []}})

    sent = _slow_zendesk(module, clock, ticket, scopes=["read", "write"])
    arguments = {"ticket_id": 1, "tags": ["x"]}
    with TestClient(module.app, base_url=BASE_URL) as client:
        assert _tool_text(client, ZENDESK_FLOW_PATH, "add_ticket_tags", arguments)[0] is False
        put = [request for _, request in sent if request.method == "PUT"]
        assert put[0].extensions["timeout"] == httpx.Timeout(30.0).as_dict()
        sent.clear()
        get_latency["value"] = 21.0
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "add_ticket_tags", arguments)
    assert is_error is True and "time budget" in text and "no change was made" in text
    assert [request.method for _, request in sent] == ["GET"]


def test_flow_single_get_errors_at_budget(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single slow GET on /zendesk/mcp gives a tool error that names the budget."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, lambda request: (40.0, httpx.Response(200, json={})))
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket", {"ticket_id": 1})
    assert is_error is True and "25 s time budget" in text


def _paged(kind: str, pages: int, latency: float) -> Callable[[httpx.Request], tuple]:
    """Stub `pages` pages of comments or audits for any ticket; item ids are 10*page + 1..2."""

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Serve the requested page, linking to the next as an absolute Zendesk URL."""
        page = int(request.url.params.get("page", "1"))
        items = [{"id": 10 * page + n, "events": [{"type": "Change"}]} for n in (1, 2)]
        next_page = None
        if page < pages:
            next_page = f"https://example.zendesk.com{request.url.path}?page={page + 1}"
        return latency, httpx.Response(200, json={kind: items, "next_page": next_page})

    return answer


def _ids(items: list[dict]) -> list[int]:
    """Return the ids of a list of comments or audits."""
    return [item["id"] for item in items]


@pytest.mark.parametrize(
    "tool,kind", [("get_ticket_comments", "comments"), ("get_ticket_audits", "audits")]
)
def test_flow_resume_with_cursor_gets_everything(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tool: str, kind: str
) -> None:
    """Calls chained by next_cursor return every item once, in order; the last has no cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _paged(kind, pages=5, latency=10.0))
    collected: list[dict] = []
    cursors: list[str] = []
    arguments: dict = {"ticket_id": 7}
    with TestClient(module.app, base_url=BASE_URL) as client:
        for _ in range(5):
            is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
            assert is_error is False, text
            body = json.loads(text)
            if isinstance(body, list):
                collected += body
                break
            collected += body[kind]
            cursors.append(body["next_cursor"])
            arguments = {"ticket_id": 7, "cursor": body["next_cursor"]}
    assert _ids(collected) == [10 * page + n for page in range(1, 6) for n in (1, 2)]
    assert cursors == [f"/tickets/7/{kind}.json?page=3", f"/tickets/7/{kind}.json?page=5"]


def test_flow_bulk_unfinished_resumes_to_full_threads(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bulk partial threads plus get_ticket_comments(cursor) give each full thread exactly once."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _paged("comments", pages=3, latency=3.0))
    full = [10 * page + n for page in range(1, 4) for n in (1, 2)]
    with TestClient(module.app, base_url=BASE_URL) as client:
        call = {"ticket_ids": [1, 2, 3, 4], "max_concurrency": 1}
        body = json.loads(
            _tool_text(client, ZENDESK_FLOW_PATH, "get_comments_for_tickets", call)[1]
        )
        assert body.get("unfinished")
        for ticket_id, cursor in body["unfinished"].items():
            arguments = {"ticket_id": int(ticket_id), "cursor": cursor}
            rest = json.loads(
                _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket_comments", arguments)[1]
            )
            body["comments_by_ticket"][ticket_id] += rest
    for ticket_id in ("1", "2", "3", "4"):
        assert _ids(body["comments_by_ticket"][ticket_id]) == full, ticket_id


@pytest.mark.parametrize(
    "cursor",
    [
        "https://evil.com/api/v2/tickets/7/comments.json?page=2",
        "//evil.com/tickets/7/comments.json",
        "/tickets/8/comments.json?page=2",
        "/tickets/7/comments.json?page=2&include=users",
        "/tickets/7/comments.json\n?page=2",
        "/tickets/7/comments.json#x",
        "/tickets/7/comments.json?page=2;include=users",
        "/tickets/7/comments.json?page=2&page=3",
        "/tickets/7/comments.json?page=2\x7f",
    ],
)
def test_bad_cursor_is_refused_before_any_request(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, cursor: str
) -> None:
    """A cursor this tool did not issue gets an error and sends nothing to any host."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    arguments = {"ticket_id": 7, "cursor": cursor}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, CLAUDE_CODE_PATH, "get_ticket_comments", arguments)
    assert is_error is False and "Invalid or expired cursor" in _json_object(text).get("error", "")
    assert seen == []


@pytest.mark.parametrize(
    "tool,kind", [("get_ticket_comments", "comments"), ("get_ticket_audits", "audits")]
)
def test_expired_cursor_gets_cursor_error(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tool: str, kind: str
) -> None:
    """When Zendesk rejects a well-formed cursor (400), the tool says to start over."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, lambda request: (0.1, httpx.Response(400, json={})))
    arguments = {"ticket_id": 7, "cursor": f"/tickets/7/{kind}.json?page[after]=gone"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
    assert is_error is False and "start over" in _json_object(text).get("error", "")


def test_cursor_param_is_http_only(load_server: Callable[..., ModuleType]) -> None:
    """HTTP schemas offer `cursor`; stdio schemas and descriptions are exactly as before."""
    http = {tool.name: tool for tool in anyio.run(_load_http(load_server).mcp.list_tools)}
    stdio_module = load_server(MCP_TRANSPORT=None)
    stdio = {tool.name: tool for tool in anyio.run(stdio_module.mcp.list_tools)}
    stdio_params = {
        "get_ticket_comments": ["ticket_id"],
        "get_ticket_audits": ["ticket_id"],
        "export_search_results": ["query", "max_results"],
    }
    for name, params in stdio_params.items():
        assert "cursor" in http[name].inputSchema["properties"], name
        assert "next_cursor" in http[name].description, name
        assert list(stdio[name].inputSchema["properties"]) == params, name
        assert stdio[name].description == getattr(stdio_module, name).__doc__, name


@pytest.mark.parametrize(
    "tool", ["get_ticket_comments", "get_ticket_audits", "export_search_results"]
)
def test_resume_note_covers_retry_and_uncut_result(
    load_server: Callable[..., ModuleType], tool: str
) -> None:
    """The HTTP resume note says to retry a no-progress error and names the uncut result."""
    http_tools = {item.name: item for item in anyio.run(_load_http(load_server).mcp.list_tools)}
    description = " ".join(http_tools[tool].description.split())
    assert "A call that is not cut short returns the usual result." in description
    assert (
        "If an `error` says to retry with the same `cursor`, call again with that same `cursor`."
        in description
    )
    assert "Any other response without `next_cursor` ends the chain" in description


@pytest.mark.parametrize("tool", ["get_ticket_comments", "get_ticket_audits"])
def test_flow_no_progress_gives_error_without_cursor(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """If not even one page fits in the budget, the answer is an error with no next_cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, lambda request: (40.0, httpx.Response(200, json={})))
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, {"ticket_id": 7})
    body = _json_object(text)
    assert is_error is False and "nothing was fetched" in body["error"]
    assert "next_cursor" not in body


def _offset_cursor(offset: int) -> str:
    """A realistic Zendesk after_cursor: ~300 characters of base64 with `+`, `/` and `=`."""
    raw = json.dumps({"offset": offset, "pad": "~?>" * 70}).encode()
    return base64.b64encode(raw).decode()


def _cursor_offset(after_cursor: str) -> int:
    """Decode `_offset_cursor`."""
    return json.loads(base64.b64decode(after_cursor))["offset"]


def _export_stream(total: int, latency: float) -> Callable[[httpx.Request], tuple]:
    """Stub the export endpoint over `total` tickets (ids 1..total), honouring page[size].

    Its after_cursor is realistic base64 (see `_offset_cursor`); each page takes `latency` s.
    """

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Serve the page that starts at the after_cursor offset."""
        after = request.url.params.get("page[after]")
        offset = 0 if after is None else _cursor_offset(after)
        size = int(request.url.params["page[size]"])
        ids = list(range(offset + 1, min(offset + size, total) + 1))
        next_offset = offset + len(ids)
        meta = {"has_more": next_offset < total, "after_cursor": _offset_cursor(next_offset)}
        body = {"results": [{"id": ticket_id} for ticket_id in ids], "meta": meta}
        return latency, httpx.Response(200, json=body)

    return answer


def _export_chain(client: TestClient, arguments: dict) -> tuple[list[int], list[dict]]:
    """Call export_search_results via /zendesk/mcp, following next_cursor; return ids and bodies."""
    ids: list[int] = []
    bodies: list[dict] = []
    for _ in range(10):
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", arguments)
        assert is_error is False, text
        body = json.loads(text)
        bodies.append(body)
        ids += [ticket["id"] for ticket in body["tickets"]]
        if "next_cursor" not in body:
            break
        arguments = {**arguments, "cursor": body["next_cursor"]}
    return ids, bodies


def test_flow_export_resume_gets_everything(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Chained export calls return every ticket once, in order, with realistic Zendesk cursors."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _export_stream(total=6000, latency=1.0))
    with TestClient(module.app, base_url=BASE_URL) as client:
        ids, bodies = _export_chain(client, {"query": "x", "max_results": 10000})
    assert ids == list(range(1, 6001))
    assert len(bodies) == 3 and "warning" not in bodies[-1]
    assert {request.url.params["page[size]"] for _, request in sent} == {"100"}
    assert bodies[0]["next_cursor"].split(".")[2] == "7500"


def test_flow_export_resume_keeps_max_results_total(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """max_results caps the whole chain: the cursor carries the remaining allowance."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _export_stream(total=5000, latency=2.0))
    with TestClient(module.app, base_url=BASE_URL) as client:
        ids, bodies = _export_chain(client, {"query": "x", "max_results": 2500})
    assert ids == list(range(1, 2501))
    assert "the max_results limit" in bodies[-1]["warning"] and "next_cursor" not in bodies[-1]


def test_flow_export_page_cap_resumes(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With 100-ticket flow pages, the 50-page cap ends a call with a cursor, not a dead end."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _export_stream(total=6000, latency=0.1))
    with TestClient(module.app, base_url=BASE_URL) as client:
        ids, bodies = _export_chain(client, {"query": "x", "max_results": 10000})
    assert ids == list(range(1, 6001))
    assert "request cap" in bodies[0].get("warning", "") and bodies[0]["returned_count"] == 5000


def test_stdio_export_keeps_1000_pages(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Guard: stdio still asks for 1,000-ticket pages and returns no cursor."""
    module = load_server()
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _export_stream(total=1500, latency=1.0))
    _, text = anyio.run(_call_in_memory_args, module, "export_search_results", {"query": "x"})
    assert [request.url.params["page[size]"] for _, request in sent] == ["1000"]
    assert json.loads(text)["returned_count"] == 1000


def _cut_export_cursor(client: TestClient) -> str:
    """Make one export call that the budget cuts, and return its next_cursor."""
    _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    body = json.loads(text)
    assert "next_cursor" in body, text
    return body["next_cursor"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda parts: parts[3],  # Zendesk's cursor alone
        lambda parts: ".".join([parts[0], "0" * 16, *parts[2:]]),  # another query's digest
        lambda parts: ".".join([*parts[:2], "10001", parts[3]]),
        lambda parts: ".".join([*parts[:2], "0", parts[3]]),
        lambda parts: ".".join(parts) + "&query=y",
        lambda parts: ".".join([*parts[:3], "https://evil.com/x"]),
    ],
    ids=["bare", "other_query", "over_limit", "zero", "query_suffix", "url"],
)
def test_bad_export_cursor_is_refused_before_any_request(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[list[str]], str],
) -> None:
    """A malformed, foreign-query or over-limit export cursor sends nothing."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _export_stream(total=5000, latency=10.0))
    with TestClient(module.app, base_url=BASE_URL) as client:
        parts = _cut_export_cursor(client).split(".", 3)
        sent.clear()
        arguments = {"query": "x", "cursor": mutate(parts)}
        is_error, text = _tool_text(client, CLAUDE_CODE_PATH, "export_search_results", arguments)
    assert is_error is False and "Invalid or expired cursor" in _json_object(text).get("error", "")
    assert sent == []


def test_expired_export_cursor_gets_cursor_error(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Zendesk rejects an expired after_cursor (400), the tool says to start over."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    expired = {"now": False}
    pages = _export_stream(total=5000, latency=10.0)

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Serve pages until the cursor 'expires', then answer 400."""
        if expired["now"]:
            return 0.1, httpx.Response(400, json={})
        return pages(request)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        cursor = _cut_export_cursor(client)
        expired["now"] = True
        arguments = {"query": "x", "cursor": cursor}
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", arguments)
    assert is_error is False and "start over" in _json_object(text).get("error", "")


def test_flow_export_unresumable_cursor_is_not_handed_out(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Zendesk's after_cursor would fail our own check, no next_cursor is given."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)

    def odd_cursor(request: httpx.Request) -> tuple[float, httpx.Response]:
        """10 s pages whose after_cursor has a character the cursor check refuses."""
        meta = {"has_more": True, "after_cursor": "abc!def"}
        return 10.0, httpx.Response(200, json={"results": [{"id": 1}], "meta": meta})

    _slow_zendesk(module, clock, odd_cursor)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    body = json.loads(text)
    assert body["returned_count"] == 2 and "next_cursor" not in body
    assert "cannot be resumed" in body["warning"]


def test_flow_export_no_progress_gives_error_without_cursor(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If not even one export page fits in the budget, the answer is an error with no cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _export_stream(total=10, latency=40.0))
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    body = _json_object(text)
    assert "nothing was fetched" in body["error"] and "next_cursor" not in body


def test_flow_no_progress_on_resume_says_keep_cursor(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed call that gets no page in time says to retry with the same cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    latency = {"value": 10.0}

    def pages(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Paged comments at the current latency."""
        return latency["value"], _paged("comments", pages=5, latency=0)(request)[1]

    _slow_zendesk(module, clock, pages)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket_comments", {"ticket_id": 7})
        cursor = _json_object(text).get("next_cursor")
        latency["value"] = 40.0
        arguments = {"ticket_id": 7, "cursor": cursor}
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket_comments", arguments)
    body = json.loads(text)
    assert "Retry with the same `cursor`" in body["error"] and "next_cursor" not in body


@pytest.mark.parametrize(
    "next_page,resumable",
    [
        ("?page=3&per_page=100", True),
        ("?page%5Bafter%5D=xyz%3D%3D&page%5Bsize%5D=100", True),
        ("?page=3&include=users", False),
    ],
)
def test_flow_comments_cursor_round_trip(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    next_page: str,
    resumable: bool,
) -> None:
    """A next_page shape we would refuse is never handed out; accepted ones resume cleanly."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    calls = itertools.count()

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Page 1 links to `next_page`; the next page ends the thread."""
        first = next(calls) == 0
        link = f"https://example.zendesk.com/api/v2/tickets/7/comments.json{next_page}"
        body = {
            "comments": [{"id": 1 if first else 2}],
            "next_page": link if first else None,
        }
        return (10.0 if first else 20.0), httpx.Response(200, json=body)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket_comments", {"ticket_id": 7})
        body = _json_object(text)
        if not resumable:
            assert "next_cursor" not in body and "cannot be resumed" in body["warning"]
            return
        arguments = {"ticket_id": 7, "cursor": body["next_cursor"]}
        _, rest = _tool_text(client, ZENDESK_FLOW_PATH, "get_ticket_comments", arguments)
    assert _ids(body["comments"]) + _ids(json.loads(rest)) == [1, 2]


def test_stdio_empty_edge_responses_unchanged(load_server: Callable[..., ModuleType]) -> None:
    """Guard: stdio keeps the empty-result strings when other caps set truncated_by."""
    module = load_server()

    def empty_more(request: httpx.Request) -> httpx.Response:
        """An empty page that claims more but gives no cursor."""
        return httpx.Response(200, json={"records": [], "results": [], "meta": {"has_more": True}})

    module._client = httpx.Client(
        base_url=module.BASE_URL, timeout=30.0, transport=httpx.MockTransport(empty_more)
    )
    _, tags = anyio.run(_call_in_memory_args, module, "list_content_tags", {})
    assert tags == "No content tags found."


def _articles(request: httpx.Request) -> tuple[float, httpx.Response]:
    """One article per 10 s page, always with a next page."""
    page = int(request.url.params.get("page", "1"))
    body = {"results": [{"id": page}], "count": 99, "next_page": f"{BASE_URL}/x?page={page + 1}"}
    return 10.0, httpx.Response(200, json=body)


def _tags(request: httpx.Request) -> tuple[float, httpx.Response]:
    """One content tag per 10 s page, always with more."""
    body = {"records": [{"id": "a", "name": "n"}], "meta": {"has_more": True, "after_cursor": "c"}}
    return 10.0, httpx.Response(200, json=body)


@pytest.mark.parametrize(
    "tool,arguments,key",
    [("search_articles", {"query": "x"}, "articles"), ("list_content_tags", {}, "content_tags")],
)
@pytest.mark.parametrize("latency", [10.0, 40.0], ids=["partial", "zero"])
def test_flow_articles_and_tags_partial_without_cursor(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict,
    key: str,
    latency: float,
) -> None:
    """Budget stops give partial results (possibly none) and a 'may be incomplete' warning."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    stub = _articles if tool == "search_articles" else _tags
    _slow_zendesk(module, clock, lambda request: (latency, stub(request)[1]))
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
    body = _json_object(text)
    assert len(body[key]) == (2 if latency == 10.0 else 0)
    assert "may be incomplete" in body["warning"] and "next_cursor" not in body


def _fake_attachment_stream(
    clock: _FakeClock, record: list, chunks: list[bytes], timeout_error: bool = False
) -> Callable[..., Any]:
    """Stand in for httpx.stream: each chunk takes 10 s on the fake clock."""

    def fake_stream(method: str, url: str, **kwargs: Any) -> Any:
        """Record the call's kwargs and stream the chunks, or raise ReadTimeout."""
        record.append(kwargs)
        if timeout_error:
            raise httpx.ReadTimeout("slow")

        def body() -> Iterator[bytes]:
            """Yield chunks, advancing the clock."""
            for chunk in chunks:
                clock.advance(10.0)
                yield chunk

        response = httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=body(),
            request=httpx.Request(method, url),
        )
        return contextlib.nullcontext(response)

    return fake_stream


@pytest.mark.parametrize("timeout_error", [False, True], ids=["mid_stream", "read_timeout"])
def test_flow_fetch_attachment_stops_by_budget(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, timeout_error: bool
) -> None:
    """The flow gets kind stopped_by_time_budget; /claude/mcp keeps 60 s and main's behaviour."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _tags)
    record: list[dict] = []
    fake = _fake_attachment_stream(clock, record, [b"a" * 10] * 5, timeout_error)
    monkeypatch.setattr(module.httpx, "stream", fake)
    attachment = {"url": "https://example.zendesk.com/a"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        flow = _tool_text(client, ZENDESK_FLOW_PATH, "fetch_attachment", attachment)
        claude = _tool_text(client, CLAUDE_CODE_PATH, "fetch_attachment", attachment)
        refused = _tool_text(
            client, ZENDESK_FLOW_PATH, "fetch_attachment", {"url": "https://evil.com/a"}
        )
    assert _json_object(flow[1]).get("kind") == "stopped_by_time_budget"
    assert record[0]["timeout"] <= 25.0 and record[0]["follow_redirects"] is False
    assert record[1]["timeout"] == 60.0
    assert claude[0] is timeout_error  # the read timeout still raises off the flow path
    assert "error" in json.loads(refused[1]) and len(record) == 2


def test_flow_retry_after_within_budget_sleeps(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Retry-After that fits in the budget is honoured once, then the call completes."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    calls = itertools.count()

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """429 asking for 3 s, then one final page."""
        if next(calls) == 0:
            return 1.0, httpx.Response(429, headers={"retry-after": "3"})
        page = {"results": [{"id": 1}], "meta": {"has_more": False}}
        return 1.0, httpx.Response(200, json=page)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    assert clock.sleeps == [3] and json.loads(text)["returned_count"] == 1


def test_http_descriptions_document_budget_and_cursor(
    load_server: Callable[..., ModuleType],
) -> None:
    """HTTP descriptions carry the notes and a `cursor:` Args line; stdio ones equal __doc__."""
    http = {tool.name: tool for tool in anyio.run(_load_http(load_server).mcp.list_tools)}
    for name in ("export_search_results", "get_ticket_comments", "get_ticket_audits"):
        lines = [line.strip() for line in http[name].description.splitlines()]
        args_at = lines.index("Args:")
        assert any(line.startswith("cursor: HTTP only.") for line in lines[args_at:]), name
    export = " ".join(http["export_search_results"].description.split())
    assert "pass the same `query`" in export and "`max_results` is ignored" in export
    assert "the time budget or the 50-page cap" in export
    assert "if it has a `warning` or an `error`, the result is incomplete" in export
    audits = [line.strip() for line in http["get_ticket_audits"].description.splitlines()]
    assert audits[audits.index("Args:") + 1] == "ticket_id: The Zendesk ticket ID."
    assert "`returned_count`" in export
    assert "`unfinished`" in http["get_comments_for_tickets"].description
    bulk = " ".join(http["get_comments_for_tickets"].description.split())
    assert "cannot be resumed is listed in `errors`" in bulk
    assert "over the size limit has a `warning` and no `unfinished`" in bulk
    comments = " ".join(http["get_ticket_comments"].description.split())
    assert "or a value from get_comments_for_tickets' `unfinished`" in comments
    assert "instead of the content" in http["fetch_attachment"].description
    for name in ("search_articles", "list_content_tags"):
        assert "stops after 25 s" in http[name].description, name
    assert "stops after 25 s or at the 50-page cap" in export
    assert "stops after 25 s and" in " ".join(http["get_ticket_comments"].description.split())
    budget_tools = (
        "export_search_results",
        "get_ticket_comments",
        "get_ticket_audits",
        "get_comments_for_tickets",
        "fetch_attachment",
        "search_articles",
        "list_content_tags",
    )
    for name in budget_tools:
        lines = http[name].description.splitlines()
        body_indent = re.match(r"[ \t]*", next(line for line in lines[1:] if line.strip()))
        note = next(line for line in lines if "On /zendesk/mcp" in line)
        # Python 3.13+ strips docstring indentation; older versions keep it.
        assert note.startswith(body_indent.group() + "On /zendesk/mcp"), name
    stdio_module = load_server(MCP_TRANSPORT=None)
    for tool in anyio.run(stdio_module.mcp.list_tools):
        assert tool.description == getattr(stdio_module, tool.name).__doc__, tool.name
        assert "/zendesk/mcp" not in tool.description, tool.name


@pytest.mark.parametrize("value", ["abc", "0", "30", "nan", "0.5"])
def test_flow_budget_env_validation(load_server: Callable[..., ModuleType], value: str) -> None:
    """HTTP mode refuses a budget outside 1-29 at import; stdio ignores the variable."""
    with pytest.raises(ValueError, match="MCP_ZENDESK_FLOW_BUDGET_SECONDS"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_ZENDESK_FLOW_BUDGET_SECONDS=value)
    load_server(MCP_TRANSPORT=None, MCP_ZENDESK_FLOW_BUDGET_SECONDS=value)
    http = _load_http(load_server, MCP_ZENDESK_FLOW_BUDGET_SECONDS="3")
    assert http.ZENDESK_FLOW_BUDGET_SECONDS == 3.0


def test_flow_export_resume_ending_empty_stays_json(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed export whose page is empty answers JSON with returned_count 0, not a string."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = _export_stream(total=200, latency=10.0)

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Two full pages, then an empty last page (Zendesk's count shrank)."""
        if (
            request.url.params.get("page[after]")
            and _cursor_offset(request.url.params["page[after]"]) >= 200
        ):
            meta = {"has_more": False, "after_cursor": None}
            return 10.0, httpx.Response(200, json={"results": [], "meta": meta})
        latency, response = pages(request)
        body = response.json()
        body["meta"]["has_more"] = True
        return latency, httpx.Response(200, json=body)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        ids, bodies = _export_chain(client, {"query": "x"})
    assert ids == list(range(1, 201))
    assert bodies[-1] == {"returned_count": 0, "tickets": []}


@pytest.mark.parametrize(
    "tool,kind", [("get_ticket_comments", "comments"), ("get_ticket_audits", "audits")]
)
def test_flow_later_page_error_is_not_a_cursor_error(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tool: str, kind: str
) -> None:
    """On a resumed call, a 404 on a later page is not reported as an invalid cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    paged = _paged(kind, pages=5, latency=1.0)

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Page 3 is missing; the others are served."""
        if request.url.params.get("page") == "3":
            return 1.0, httpx.Response(404, json={"error": "gone"})
        return paged(request)

    _slow_zendesk(module, clock, answer)
    arguments = {"ticket_id": 7, "cursor": f"/tickets/7/{kind}.json?page=2"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
    assert "Invalid or expired cursor" not in text
    assert is_error is True and "404" in text


def test_flow_export_later_page_error_is_not_a_cursor_error(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a resumed export, a 500 on a later page is not reported as an invalid cursor."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = _export_stream(total=5000, latency=10.0)
    fail_after = {"offset": None}

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Serve pages; once armed, fail every page after the first one of the call."""
        after = request.url.params.get("page[after]")
        if (
            fail_after["offset"] is not None
            and after
            and _cursor_offset(after) > fail_after["offset"]
        ):
            return 1.0, httpx.Response(422, json={"error": "x"})
        return pages(request)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        cursor = _cut_export_cursor(client)
        fail_after["offset"] = 200
        arguments = {"query": "x", "cursor": cursor}
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", arguments)
    assert "Invalid or expired cursor" not in text
    assert is_error is True and "422" in text


def test_audits_bad_cursor_is_refused_before_any_request(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comments cursor sent to get_ticket_audits is refused with no request sent."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    arguments = {"ticket_id": 7, "cursor": "/tickets/7/comments.json?page=2"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, CLAUDE_CODE_PATH, "get_ticket_audits", arguments)
    assert is_error is False and "Invalid or expired cursor" in _json_object(text).get("error", "")
    assert seen == []


def test_overlong_cursor_is_refused(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A comments cursor over 2,048 characters is refused before any request."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    arguments = {"ticket_id": 7, "cursor": "/tickets/7/comments.json?page=" + "9" * 2100}
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, CLAUDE_CODE_PATH, "get_ticket_comments", arguments)
    assert "Invalid or expired cursor" in _json_object(text).get("error", "") and seen == []


def test_call_deadline_fails_closed_without_request(load_server: Callable[..., ModuleType]) -> None:
    """HTTP mode with no MCP request (e.g. a worker thread) refuses to guess a deadline."""
    http = _load_http(load_server)
    with pytest.raises(RuntimeError, match="Cannot tell which endpoint"):
        http._call_deadline()
    assert load_server(MCP_TRANSPORT=None)._call_deadline() is None


def _big_threads(request: httpx.Request) -> tuple[float, httpx.Response]:
    """Two 2 s pages per ticket; page 1 holds one comment of about 700 KB."""
    if request.url.params.get("page") == "2":
        return 2.0, httpx.Response(200, json={"comments": [{"id": 2}], "next_page": None})
    next_page = f"https://example.zendesk.com{request.url.path}?page=2"
    comment = {"id": 1, "body": "a" * 700_000}
    return 2.0, httpx.Response(200, json={"comments": [comment], "next_page": next_page})


def test_flow_bulk_oversized_keeps_warning(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Over ~4 MB with threads cut by the budget: a `warning`, and no `unfinished` cursors."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _big_threads)
    call = {"ticket_ids": list(range(1, 9)), "max_concurrency": 1}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_comments_for_tickets", call)
    body = _json_object(text)
    assert is_error is False and "comment_counts" in body and "unfinished" not in body
    assert "time budget" in body.get("warning", "")


def test_flow_bulk_unresumable_ticket_goes_to_errors(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bulk thread the budget cuts at a page we cannot resume goes to `errors`, not results."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    calls = itertools.count()

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Page 1 (10 s) links to a page with a key we refuse; that page takes 20 s."""
        first = next(calls) == 0
        link = "https://example.zendesk.com/api/v2/tickets/7/comments.json?page=2&include=users"
        body = {"comments": [{"id": 1 if first else 2}], "next_page": link if first else None}
        return (10.0 if first else 20.0), httpx.Response(200, json=body)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        call = {"ticket_ids": [7]}
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "get_comments_for_tickets", call)
    body = _json_object(text)
    assert "cannot be resumed" in body.get("errors", {}).get("7", "")
    assert "7" not in body["comments_by_ticket"] and "unfinished" not in body


def test_flow_export_missing_after_cursor_ends_chain_with_warning(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resumed export page with more results but no after_cursor ends the chain with a warning."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = _export_stream(total=5000, latency=10.0)

    def answer(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Serve pages; from offset 200 on, a page claims more but carries no after_cursor."""
        latency, response = pages(request)
        body = response.json()
        after = request.url.params.get("page[after]")
        if after and _cursor_offset(after) >= 200:
            body["meta"]["after_cursor"] = None
        return latency, httpx.Response(200, json=body)

    _slow_zendesk(module, clock, answer)
    with TestClient(module.app, base_url=BASE_URL) as client:
        ids, bodies = _export_chain(client, {"query": "x"})
    assert ids == list(range(1, 301))
    assert len(bodies) == 2 and "next_cursor" in bodies[0]
    assert "a missing cursor" in bodies[-1]["warning"] and "next_cursor" not in bodies[-1]


# Round 2: guards on branches the work review found untested


def test_flow_fetch_attachment_no_time_left_sends_nothing(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With under 1 s left after the token check, fetch_attachment opens no stream."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _tags, token_check_seconds=24.5)
    record: list[dict] = []
    monkeypatch.setattr(module.httpx, "stream", _fake_attachment_stream(clock, record, [b"a"]))
    attachment = {"url": "https://example.zendesk.com/a"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "fetch_attachment", attachment)
    body = _json_object(text)
    assert body.get("kind") == "stopped_by_time_budget" and body.get("bytes_read") == 0
    assert record == []


def _one_ticket_pages(request: httpx.Request) -> tuple[float, httpx.Response]:
    """0.1 s export pages of one ticket each, always with more."""
    page = {"results": [{"id": 1}], "meta": {"has_more": True, "after_cursor": "c"}}
    return 0.1, httpx.Response(200, json=page)


@pytest.mark.parametrize("path", [CLAUDE_CODE_PATH, "stdio"])
def test_page_cap_gives_no_cursor_off_flow_path(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    """Off the flow, the 50-page cap ends the export with a warning and no next_cursor."""
    module = load_server() if path == "stdio" else _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    sent = _slow_zendesk(module, clock, _one_ticket_pages)
    if path == "stdio":
        _, text = anyio.run(_call_in_memory_args, module, "export_search_results", {"query": "x"})
    else:
        with TestClient(module.app, base_url=BASE_URL) as client:
            _, text = _tool_text(client, path, "export_search_results", {"query": "x"})
    body = _json_object(text)
    assert body["returned_count"] == 50 and "next_cursor" not in body
    assert "request cap" in body.get("warning", "")
    assert sent[0][1].url.params["page[size]"] == "1000"  # flow pages are 100


@pytest.mark.parametrize("path", [ZENDESK_FLOW_PATH, "stdio"])
@pytest.mark.parametrize(
    "tool,arguments",
    [
        ("get_ticket_comments", {"ticket_id": 7}),
        ("get_ticket_audits", {"ticket_id": 7}),
        ("export_search_results", {"query": "x"}),
    ],
)
def test_first_page_404_without_cursor_is_a_plain_error(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    tool: str,
    arguments: dict,
) -> None:
    """Guard: a call from the start whose first page 404s raises as on main, not a cursor error."""
    module = load_server() if path == "stdio" else _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, lambda request: (0.1, httpx.Response(404, json={})))
    if path == "stdio":
        is_error, text = anyio.run(_call_in_memory_args, module, tool, arguments)
    else:
        with TestClient(module.app, base_url=BASE_URL) as client:
            is_error, text = _tool_text(client, path, tool, arguments)
    assert "Invalid or expired cursor" not in text
    assert is_error is True and "404" in text


@pytest.mark.parametrize("status", [500, 429])
@pytest.mark.parametrize(
    "tool", ["get_ticket_comments", "get_ticket_audits", "export_search_results"]
)
def test_resumed_first_request_non_cursor_status_is_not_a_cursor_error(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tool: str, status: int
) -> None:
    """A 500, or a 429 after the last retry, on a resumed call's first request is a plain error."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    headers = {"retry-after": "1"} if status == 429 else {}
    sent = _slow_zendesk(
        module, clock, lambda request: (0.1, httpx.Response(status, headers=headers, json={}))
    )
    if tool == "export_search_results":
        arguments = {"query": "x", "cursor": module._export_cursor("x", 100, "abc")}
    else:
        kind = "comments" if tool == "get_ticket_comments" else "audits"
        arguments = {"ticket_id": 7, "cursor": f"/tickets/7/{kind}.json?page=2"}
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
    assert "Invalid or expired cursor" not in text
    assert is_error is True and str(status) in text and sent


@pytest.mark.parametrize(
    "tool,arguments,level,method",
    [
        ("create_ticket", {"subject": "s", "body": "b"}, "management", "POST"),
        ("add_ticket_tags", {"ticket_id": 1, "tags": ["x"]}, "management", "PUT"),
        ("delete_ticket", {"ticket_id": 1}, "admin", "DELETE"),
    ],
)
def test_flow_write_refused_with_under_5_s_left(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    tool: str,
    arguments: dict,
    level: str,
    method: str,
) -> None:
    """POST, PUT and DELETE are all refused, unsent, when the token check leaves 4 s."""
    module = _load_http(load_server, ACCESS_LEVEL=level)
    clock = _with_clock(module, monkeypatch)

    def ticket(request: httpx.Request) -> tuple[float, httpx.Response]:
        """Any request takes 1 s and returns a ticket."""
        return 1.0, httpx.Response(200, json={"ticket": {"id": 1, "subject": "s", "tags": []}})

    sent = _slow_zendesk(module, clock, ticket, token_check_seconds=21.0, scopes=["read", "write"])
    with TestClient(module.app, base_url=BASE_URL) as client:
        is_error, text = _tool_text(client, ZENDESK_FLOW_PATH, tool, arguments)
    assert method not in [request.method for _, request in sent]
    assert is_error is True and "no change was made" in text


def test_flow_retry_after_just_over_time_left_does_not_sleep(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a Retry-After that fits but leaves under 1 s for the retry is not slept."""
    module = _load_http(load_server)
    clock = _with_clock(module, monkeypatch)
    pages = itertools.count(1)

    def then_429(request: httpx.Request) -> tuple[float, httpx.Response]:
        """First page at 10 s; then a 429 at 11.5 s asking for 13 s (13.5 s are left)."""
        if next(pages) == 1:
            return _export_page(request)
        return 1.5, httpx.Response(429, headers={"retry-after": "13"})

    _slow_zendesk(module, clock, then_429)
    with TestClient(module.app, base_url=BASE_URL) as client:
        _, text = _tool_text(client, ZENDESK_FLOW_PATH, "export_search_results", {"query": "x"})
    assert _json_object(text)["returned_count"] == 1
    assert clock.sleeps == []


def test_http_resumable_requires_cursor_parameter(load_server: Callable[..., ModuleType]) -> None:
    """`_http_resumable` refuses a tool that has no `cursor` parameter."""
    module = _load_http(load_server)

    def no_cursor(ticket_id: int) -> str:
        """A tool without `cursor`."""
        return ""

    with pytest.raises(RuntimeError, match="expected a `cursor` parameter"):
        module._http_resumable("`x`")(no_cursor)


def test_stdio_export_ticket_comments_writes_whole_thread(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Stdio export_ticket_comments still follows every page with no deadline."""
    module = load_server()
    clock = _with_clock(module, monkeypatch)
    _slow_zendesk(module, clock, _paged("comments", pages=3, latency=20.0))
    output = tmp_path / "thread.json"
    arguments = {"ticket_id": 7, "output_path": str(output)}
    is_error, text = anyio.run(_call_in_memory_args, module, "export_ticket_comments", arguments)
    assert is_error is False and _json_object(text)["comment_count"] == 6
    assert _ids(json.loads(output.read_text())) == [11, 12, 21, 22, 31, 32]
