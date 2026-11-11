"""Offline tests for the stdio/HTTP transport split; each test loads its own copy of server.py.

The copy lives in a temp dir, so no real `.env` is loaded. HTTP is served through Starlette's
`TestClient` and Zendesk is stubbed with `httpx.MockTransport`; nothing reaches the network.
"""

import base64
import importlib.util
import itertools
import os
import shutil
from collections.abc import Callable
from pathlib import Path
from types import ModuleType

import anyio
import httpx
import pytest
import uvicorn
from mcp.server.fastmcp import FastMCP
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
