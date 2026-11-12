"""Offline tests for the stdio/HTTP transport split; each test loads its own copy of server.py.

The copy lives in a temp dir, so no real `.env` is loaded. HTTP is served through Starlette's
`TestClient` and Zendesk is stubbed with `httpx.MockTransport`; nothing reaches the network.
"""

import ast
import contextlib
import importlib.util
import itertools
import json
import os
import re
import shutil
from collections.abc import Callable
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
