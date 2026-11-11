"""Offline tests for the stdio/HTTP transport split; each test loads its own copy of server.py.

The copy lives in a temp dir, so no real `.env` is loaded. HTTP is served through Starlette's
`TestClient` and Zendesk is stubbed with `httpx.MockTransport`; nothing reaches the network.
"""

import base64
import importlib.util
import itertools
import json
import os
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
MCP_HEADERS = {"accept": "application/json, text/event-stream", "content-type": "application/json"}
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
    try:
        module = load_server(MCP_TRANSPORT="streamable-http", **env)
    except KeyError as error:
        pytest.fail(f"HTTP mode must import without Zendesk credentials: {error!r}")
    assert isinstance(getattr(module, "app", None), Starlette), "HTTP mode must build `app`"
    return module


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

    def handler(request: httpx.Request) -> httpx.Response:
        """Record the request and answer with a minimal ticket."""
        seen.append(request)
        return httpx.Response(200, json={"ticket": {"id": 1}})

    stub = httpx.Client(base_url=module.BASE_URL, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module, "_client", stub)
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
    """HTTP mode refuses a non-loopback MCP_HOST and loads on loopback."""
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


def test_http_tool_call_uses_env_basic_auth(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With env creds set, an HTTP tool call sends them as basic auth."""
    module = _load_http(load_server)
    seen = _stub_zendesk(module, monkeypatch)
    with TestClient(module.app, base_url=BASE_URL) as client:
        result = _rpc(client, CLAUDE_CODE_PATH, "tools/call", GET_TICKET_CALL).json()["result"]
    assert '"id": 1' in result["content"][0]["text"]
    expected = "Basic " + base64.b64encode(b"env@example.com/token:env-key").decode("ascii")
    assert [request.headers["authorization"] for request in seen] == [expected]


@pytest.mark.parametrize(
    "missing",
    [("ZENDESK_EMAIL", "ZENDESK_API_KEY"), ("ZENDESK_API_KEY",), ("ZENDESK_EMAIL",)],
    ids=["no_creds", "only_email", "only_api_key"],
)
def test_http_tool_call_without_credentials_errors(
    load_server: Callable[..., ModuleType],
    monkeypatch: pytest.MonkeyPatch,
    missing: tuple[str, ...],
) -> None:
    """Without both env creds, an HTTP tool call errors and sends no request."""
    module = _load_http(load_server, **dict.fromkeys(missing))
    seen = _stub_zendesk(module, monkeypatch)
    with TestClient(module.app, base_url=BASE_URL) as client:
        result = _rpc(client, CLAUDE_CODE_PATH, "tools/call", GET_TICKET_CALL).json()["result"]
    assert result["isError"] is True
    assert "No Zendesk credentials for this request" in result["content"][0]["text"]
    assert seen == []


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
        transport_endpoint = flow_route.endpoint.app  # unwrap the _EndpointTag
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
            "https://MCP.Example.com:443/",
            ["mcp.example.com", "mcp.example.com:443"],
            ["https://mcp.example.com", "https://mcp.example.com:443"],
        ),
        (
            "https://mcp.example.com:8443",
            ["mcp.example.com:8443"],
            ["https://mcp.example.com:8443"],
        ),
        ("http://[2001:db8::1]:8080", ["[2001:db8::1]:8080"], ["http://[2001:db8::1]:8080"]),
        (
            "http://mcp.example.com",
            ["mcp.example.com", "mcp.example.com:80"],
            ["http://mcp.example.com", "http://mcp.example.com:80"],
        ),
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
    ],
)
def test_http_mode_rejects_bad_public_url(load_server: Callable[..., ModuleType], url: str) -> None:
    """A malformed MCP_PUBLIC_URL raises ValueError at import, naming it invalid."""
    with pytest.raises(ValueError, match="Invalid MCP_PUBLIC_URL"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_PUBLIC_URL=url)


@pytest.mark.parametrize(
    "creds", [{}, {"ZENDESK_EMAIL": None, "ZENDESK_API_KEY": None}], ids=["env_creds", "no_creds"]
)
def test_http_mode_refuses_public_url(
    load_server: Callable[..., ModuleType], creds: dict[str, str | None]
) -> None:
    """A valid MCP_PUBLIC_URL is refused at import until caller authentication exists."""
    with pytest.raises(RuntimeError, match="Refusing MCP_PUBLIC_URL"):
        load_server(
            MCP_TRANSPORT="streamable-http", MCP_PUBLIC_URL="https://mcp.example.com", **creds
        )


def test_public_url_empty_means_unset(load_server: Callable[..., ModuleType]) -> None:
    """An empty MCP_PUBLIC_URL is treated as unset: the server loads with loopback entries only."""
    module = _load_http(load_server, MCP_PUBLIC_URL="")
    loopback_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert module.mcp.settings.transport_security.allowed_hosts == loopback_hosts


def test_stdio_ignores_public_url(load_server: Callable[..., ModuleType]) -> None:
    """Stdio does not read MCP_PUBLIC_URL, so any value imports."""
    for value in ("not a url", "https://mcp.example.com"):
        assert load_server(MCP_PUBLIC_URL=value).TRANSPORT == "stdio"


def test_public_url_keeps_loopback_guard(load_server: Callable[..., ModuleType]) -> None:
    """The loopback-only MCP_HOST check runs before MCP_PUBLIC_URL is read."""
    with pytest.raises(RuntimeError, match="MCP_HOST"):
        load_server(
            MCP_TRANSPORT="streamable-http",
            MCP_HOST="0.0.0.0",
            MCP_PUBLIC_URL="https://mcp.example.com",
        )


def test_public_url_keeps_port_check(load_server: Callable[..., ModuleType]) -> None:
    """The MCP_PORT check runs before MCP_PUBLIC_URL is validated."""
    with pytest.raises(ValueError, match="Invalid MCP_PORT"):
        load_server(MCP_TRANSPORT="streamable-http", MCP_PORT="0", MCP_PUBLIC_URL="not a url")


def test_public_host_check(
    load_server: Callable[..., ModuleType], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the public settings applied, the public Host/Origin pass and others get 421/403."""
    module = _load_http(load_server)
    security = _transport_security(module)("https://mcp.example.com")
    # The settings that would apply once the refusal is lifted; the manager reads them per request.
    monkeypatch.setattr(module.mcp.session_manager, "security_settings", security)
    with TestClient(module.app, base_url=BASE_URL) as client:

        def status(path: str = CLAUDE_CODE_PATH, **headers: str) -> int:
            """Return the HTTP status of a tools/list request with the given headers."""
            return _rpc(client, path, "tools/list", **headers).status_code

        assert status(host="mcp.example.com") == 200
        assert status(ZENDESK_FLOW_PATH, host="mcp.example.com") == 200
        assert status(host="mcp.example.com:443") == 200
        assert status(host="mcp.example.com:8443") == 421
        assert status(host="evil.com") == 421
        assert status(host="127.0.0.1:8000") == 200
        assert status(host="mcp.example.com", origin="https://mcp.example.com") == 200
        assert status(host="mcp.example.com", origin="https://evil.com") == 403
        assert status(host="mcp.example.com", origin="http://mcp.example.com") == 403
