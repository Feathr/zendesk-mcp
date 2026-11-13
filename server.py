"""Zendesk MCP Server — ticket triage, management, and admin.

Access levels controlled by ACCESS_LEVEL env var:
    readonly   — search/read/count/export tickets, users, views, orgs, audits, bulk read,
        attachments, articles (18 tools)
    management — readonly + create/update tickets, comments, tags, file attachments (24 tools)
    admin      — management + user/org CRUD, merge, bulk ops, delete (33 tools)

Transport is set by MCP_TRANSPORT: `stdio` (default) or `streamable-http`. HTTP mode reads
MCP_HOST (default 127.0.0.1) and MCP_PORT (default 8000). It serves the same tools at
/zendesk/mcp (Zendesk action flows) and /claude/mcp (Claude Code), plus /health. It omits
export_ticket_comments and attach_file_to_ticket, which use the server's local filesystem (so file
attachments are stdio-only), and the levels have 17 / 22 / 31 tools.

HTTP mode is an OAuth resource server with Zendesk as the authorization server. Every MCP request
needs a Zendesk OAuth token issued to the client ZENDESK_OAUTH_CLIENT_ID (required in HTTP mode)
with the `read` scope; the server forwards that token to Zendesk and never uses ZENDESK_EMAIL or
ZENDESK_API_KEY. Write tools also need the `write` scope; ACCESS_LEVEL stays the ceiling.
MCP_PUBLIC_URL (`https://host[:port]`) is the public base URL; it is required when MCP_HOST is
not a loopback address.

Calls on /zendesk/mcp have a time budget (MCP_ZENDESK_FLOW_BUDGET_SECONDS, 1-29, default 25),
counted from the request's arrival, because Zendesk stops each tool call after 30 s. Paged reads
then return partial results with a `warning`, and some a `next_cursor` to resume from; single
reads fail with an error; a write is never cut, but is refused with under 5 s left. Stdio and
/claude/mcp have no budget.
"""

import base64
import enum
import hashlib
import inspect
import json
import math
import mimetypes
import os
import re
import time
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from http.cookiejar import CookieJar, DefaultCookiePolicy
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import parse_qsl, unquote_plus, urlparse, urlsplit

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

if TYPE_CHECKING:
    from mcp.server.auth.provider import AccessToken
    from mcp.server.transport_security import TransportSecuritySettings

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
if TRANSPORT not in ("stdio", "streamable-http"):
    raise ValueError(f"Invalid MCP_TRANSPORT: {TRANSPORT!r}. Must be stdio or streamable-http.")
if TRANSPORT == "stdio":
    EMAIL = os.environ["ZENDESK_EMAIL"]
    API_KEY = os.environ["ZENDESK_API_KEY"]
    OAUTH_CLIENT_ID: str | None = None
else:
    # HTTP mode sends each caller's own OAuth token; it never reads the API-token credentials.
    EMAIL: str | None = None
    API_KEY: str | None = None
    OAUTH_CLIENT_ID = os.environ["ZENDESK_OAUTH_CLIENT_ID"]
    if re.fullmatch(r"[1-9][0-9]*", OAUTH_CLIENT_ID) is None:
        raise ValueError(
            f"Invalid ZENDESK_OAUTH_CLIENT_ID: {OAUTH_CLIENT_ID!r}. Must be the OAuth client's "
            "numeric ID."
        )
ACCESS_LEVEL = os.environ.get("ACCESS_LEVEL", "readonly").lower()

if ACCESS_LEVEL not in ("readonly", "management", "admin"):
    raise ValueError(
        f"Invalid ACCESS_LEVEL: {ACCESS_LEVEL!r}. Must be readonly, management, or admin."
    )

BASE_URL = f"https://{SUBDOMAIN}.zendesk.com/api/v2"

ZENDESK_FLOW_PATH = "/zendesk/mcp"
CLAUDE_CODE_PATH = "/claude/mcp"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

ToolFunction = TypeVar("ToolFunction", bound=Callable[..., Any])


class Endpoint(enum.Enum):
    """Where the current tool call came from.

    STDIO is a local stdio session. ZENDESK_FLOW is `/zendesk/mcp` (Zendesk action flows) and
    CLAUDE_CODE is `/claude/mcp` (Claude Code), both in HTTP mode.
    """

    STDIO = "stdio"
    ZENDESK_FLOW = "zendesk_flow"
    CLAUDE_CODE = "claude_code"


# ASGI scope keys under which each HTTP route records its Endpoint and when the request arrived.
_ENDPOINT_SCOPE_KEY = "zendesk_mcp.endpoint"
_REQUEST_START_SCOPE_KEY = "zendesk_mcp.request_start"
_HTTP_ENDPOINTS = (Endpoint.ZENDESK_FLOW, Endpoint.CLAUDE_CODE)


def _public_url_parts(public_url: str) -> tuple[str, int | None]:
    """Validate MCP_PUBLIC_URL and return its host (IPv6 in brackets, lowercased) and port.

    Args:
        public_url: The MCP_PUBLIC_URL value, e.g. `https://mcp.example.com`.

    Returns:
        The host as it appears in a Host header, and the explicit port or None.

    Raises:
        ValueError: The scheme is not `https`; the value has whitespace or a control character
            anywhere, an `@`, a non-empty path (a trailing `/` included), query or fragment; the
            host, after lowercasing, has characters other than letters, digits, `.` and `-`
            (inside brackets: hex digits, `:` and `.`), or a trailing dot; or the port is not in
            1-65535. Empty query, fragment or port markers (`?`, `#`, `:`) and leading zeros in
            the port pass and are dropped from the result.
    """
    error = ValueError(
        f"Invalid MCP_PUBLIC_URL: {public_url!r}. Must be https://host[:port] with no path "
        "and no trailing slash."
    )
    # urlsplit silently drops tabs and newlines, so check every character first.
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in public_url):
        raise error
    try:
        parts = urlsplit(public_url)
        port = parts.port
    except ValueError:  # malformed IPv6 brackets or a non-numeric / out-of-range port
        raise error from None
    if parts.scheme != "https" or port == 0 or "@" in parts.netloc:
        raise error
    if parts.path or parts.query or parts.fragment:
        raise error
    hostname = parts.hostname or ""
    if parts.netloc.startswith("["):
        host_is_valid = re.fullmatch(r"[0-9a-f:.]+", hostname) is not None
        host = f"[{hostname}]"
    else:
        host_is_valid = re.fullmatch(r"[a-z0-9.-]+", hostname) is not None
        host_is_valid = host_is_valid and not hostname.endswith(".")
        host = hostname
    if not host_is_valid:
        raise error
    return host, port


def _public_url_entries(public_url: str) -> tuple[list[str], list[str]]:
    """Turn MCP_PUBLIC_URL into Host and Origin entries for TransportSecuritySettings.

    The SDK matches a Host or Origin exactly, or by a `base:*` pattern that needs an explicit
    port, so the bare host and the host with port 443 are listed separately.

    Args:
        public_url: The MCP_PUBLIC_URL value, e.g. `https://mcp.example.com`.

    Returns:
        The allowed Host values and the allowed Origin values.

    Raises:
        ValueError: The value fails the checks in `_public_url_parts`.
    """
    host, port = _public_url_parts(public_url)
    if port is not None and port != 443:
        return [f"{host}:{port}"], [f"https://{host}:{port}"]
    return [host, f"{host}:443"], [f"https://{host}", f"https://{host}:443"]


def _resource_base_url(host: str, port: int, public_url: str) -> str:
    """Return the base URL of this server's OAuth resource URLs and authorization metadata.

    Args:
        host: MCP_HOST.
        port: MCP_PORT.
        public_url: MCP_PUBLIC_URL; empty for none.

    Returns:
        `https://host[:port]` from MCP_PUBLIC_URL, or `http://<loopback host>:<port>` when it is
        unset. No trailing slash.

    Raises:
        ValueError: MCP_PUBLIC_URL is malformed.
        RuntimeError: MCP_HOST is not a loopback address and MCP_PUBLIC_URL is unset.
    """
    if public_url:
        public_host, public_port = _public_url_parts(public_url)
        if public_port is None or public_port == 443:
            return f"https://{public_host}"
        return f"https://{public_host}:{public_port}"
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"MCP_HOST={host!r} is not a loopback address: set MCP_PUBLIC_URL to the public "
            "https URL that clients use."
        )
    loopback_host = "[::1]" if host == "::1" else host
    return f"http://{loopback_host}:{port}"


def _transport_security(public_url: str) -> "TransportSecuritySettings":
    """Build the DNS-rebinding settings: loopback hosts plus the MCP_PUBLIC_URL host, if any.

    Args:
        public_url: The MCP_PUBLIC_URL value; empty for none.

    Returns:
        Settings with DNS-rebinding protection on.

    Raises:
        ValueError: `public_url` is set but malformed.
    """
    # Imported here so stdio keeps importing on an older mcp that lacks this module.
    from mcp.server.transport_security import TransportSecuritySettings

    # Passing any settings turns off FastMCP's localhost defaults, so list loopback explicitly.
    allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]
    if public_url:
        public_hosts, public_origins = _public_url_entries(public_url)
        allowed_hosts += public_hosts
        allowed_origins += public_origins
    return TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)


def _http_settings() -> dict[str, Any]:
    """Read and validate the HTTP-mode settings passed to FastMCP.

    Returns:
        FastMCP keyword arguments `host`, `port` and `transport_security` (loopback plus the
        MCP_PUBLIC_URL host), and `resource_base_url`, which the caller removes before FastMCP.

    Raises:
        ValueError: MCP_PORT is not a port number, or MCP_PUBLIC_URL fails the checks in
            `_public_url_parts`.
        RuntimeError: MCP_HOST is not a loopback address and MCP_PUBLIC_URL is unset.
    """
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port_raw = os.environ.get("MCP_PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"Invalid MCP_PORT: {port_raw!r}. Must be a port number.") from None
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid MCP_PORT: {port_raw!r}. Must be a port number.")
    public_url = os.environ.get("MCP_PUBLIC_URL", "")
    return {
        "host": host,
        "port": port,
        "transport_security": _transport_security(public_url),
        "resource_base_url": _resource_base_url(host, port, public_url),
    }


def _flow_budget_seconds() -> float:
    """Read MCP_ZENDESK_FLOW_BUDGET_SECONDS, the time budget for calls on /zendesk/mcp.

    Returns:
        The budget in seconds; 25 when unset.

    Raises:
        ValueError: The value is not a number from 1 to 29.
    """
    raw = os.environ.get("MCP_ZENDESK_FLOW_BUDGET_SECONDS", "25")
    try:
        budget = float(raw)
    except ValueError:
        budget = math.nan
    if not 1 <= budget <= 29:
        raise ValueError(
            f"Invalid MCP_ZENDESK_FLOW_BUDGET_SECONDS: {raw!r}. Must be a number from 1 to 29."
        )
    return budget


if TRANSPORT == "stdio":
    mcp = FastMCP("zendesk")
    ZENDESK_FLOW_BUDGET_SECONDS: float | None = None
else:
    _http_config = _http_settings()
    RESOURCE_BASE_URL: str = _http_config.pop("resource_base_url")
    ZENDESK_FLOW_BUDGET_SECONDS = _flow_budget_seconds()
    mcp = FastMCP(
        "zendesk",
        stateless_http=True,
        json_response=True,
        streamable_http_path=CLAUDE_CODE_PATH,
        **_http_config,
    )


def _current_endpoint() -> Endpoint:
    """Return the endpoint the current tool call came from.

    Call it in the tool's own thread, at tool entry, and pass the value down: the MCP request
    context is a contextvar, which does not reach ThreadPoolExecutor workers. It also works
    inside other MCP request handlers, such as tools/list.

    Returns:
        Endpoint.STDIO in stdio mode (any thread, inside or outside a request); otherwise the
        Endpoint recorded by the HTTP route that received the request.

    Raises:
        RuntimeError: HTTP mode, and the call has no HTTP request or its route did not record a
            known endpoint (including when called from a ThreadPoolExecutor worker). Never
            guesses.
    """
    if TRANSPORT == "stdio":
        return Endpoint.STDIO
    try:
        request = mcp.get_context().request_context.request
    except ValueError:  # no MCP request in this thread
        request = None
    endpoint = None if request is None else request.scope.get(_ENDPOINT_SCOPE_KEY)
    if endpoint not in _HTTP_ENDPOINTS:
        raise RuntimeError("Cannot tell which endpoint this tool call came from; refusing it.")
    return endpoint


class _DeadlineReached(Exception):
    """A call on /zendesk/mcp ran out of its time budget.

    No further request was sent: the next one would not fit in the time left, a Retry-After
    wait would not fit, or the last request timed out under its capped timeout.

    Attributes:
        partial: Results gathered before the stop, when the raiser had any.
        next_cursor: Where to resume (see `_page_cursor`), when the raiser can resume; None
            when the next page cannot be resumed through this server.
        progressed: False when no page arrived before the stop.
    """

    def __init__(
        self,
        partial: list | None = None,
        next_cursor: str | None = None,
        detail: str = "",
        progressed: bool = True,
    ) -> None:
        message = (
            f"Stopped by the {ZENDESK_FLOW_BUDGET_SECONDS:g} s time budget for Zendesk action "
            "flows (/zendesk/mcp)."
        )
        super().__init__(f"{message} {detail}" if detail else message)
        self.progressed = progressed
        self.partial = partial
        self.next_cursor = next_cursor


# Query keys Zendesk puts in a ticket comments/audits `next_page` link; a cursor may carry only these.
_CURSOR_QUERY_KEYS = frozenset({"page", "per_page", "page[size]", "page[after]", "sort_order"})
# No `next_cursor` here, so a flow that loops on it cannot spin without progress.
_NO_PROGRESS_ERROR = "Zendesk did not answer in time; nothing was fetched. Try again later."
_NO_PROGRESS_WITH_CURSOR_ERROR = (
    "Zendesk did not answer in time; nothing was fetched. Retry with the same `cursor`."
)
_CURSOR_NOT_RESUMABLE = (
    "Zendesk's next-page link cannot be resumed through this server, so no `next_cursor` is "
    "given; call again without `cursor` to start over."
)


def _no_progress_error(stop: "_DeadlineReached", cursor: str | None) -> str:
    """Build the error for a call the budget stopped before any page arrived.

    It has no `next_cursor`, so a flow that loops on `next_cursor` stops.

    Args:
        stop: The deadline exception.
        cursor: The caller's cursor, if the call was a resume.

    Returns:
        JSON with an `error`.
    """
    hint = _NO_PROGRESS_ERROR if cursor is None else _NO_PROGRESS_WITH_CURSOR_ERROR
    return json.dumps({"error": f"{stop} {hint}"}, indent=2)


def _cut_short(key: str, items: list, stop: "_DeadlineReached", next_cursor: str | None) -> str:
    """Build a comments or audits answer that the time budget cut short.

    Args:
        key: "comments" or "audits".
        items: What was fetched before the stop.
        stop: The deadline exception.
        next_cursor: Where to resume, or None when the next page cannot be resumed.

    Returns:
        JSON with the items, a `warning`, and `next_cursor` when there is one.
    """
    if next_cursor is None:
        out = {key: items, "warning": f"Incomplete. {stop} {_CURSOR_NOT_RESUMABLE}"}
    else:
        out = {
            key: items,
            "warning": f"Incomplete. {stop} Call again with `cursor` = `next_cursor`.",
            "next_cursor": next_cursor,
        }
    return json.dumps(out, indent=2)


_CURSOR_MAX_LENGTH = 2048
_CURSOR_REJECTION_STATUSES = (400, 404, 422)
_INVALID_CURSOR_ERROR = "Invalid or expired cursor; call again without `cursor` to start over."


def _page_cursor(url: str) -> str:
    """Turn the URL of the next page to fetch into an opaque resume cursor.

    Args:
        url: An absolute Zendesk `next_page` link, or a path relative to the API base.

    Returns:
        The path below `/api/v2` plus its query, e.g. `/tickets/1/comments.json?page=3`.
    """
    parts = urlsplit(url)
    path = parts.path.removeprefix("/api/v2")
    return f"{path}?{parts.query}" if parts.query else path


def _cursor_url(cursor: str, expected_path: str) -> str | None:
    """Check a caller's cursor before it is sent to Zendesk with the caller's credentials.

    Args:
        cursor: The `cursor` argument, as a previous call's `next_cursor` returned it.
        expected_path: The only path this tool may resume, e.g. `/tickets/1/comments.json`.

    Returns:
        The cursor, which is a path relative to the API base, or None when it is not one this
        tool issued: it is over _CURSOR_MAX_LENGTH characters, or has a scheme, host, fragment,
        `;`, whitespace or control character, another path, a repeated query key, or a query
        key outside `_CURSOR_QUERY_KEYS`.
    """
    if len(cursor) > _CURSOR_MAX_LENGTH or ";" in cursor:
        return None
    if any(ord(character) <= 0x20 or ord(character) == 0x7F for character in cursor):
        return None
    parts = urlsplit(cursor)
    if parts.scheme or parts.netloc or parts.fragment or parts.path != expected_path:
        return None
    keys = [key for key, _ in parse_qsl(parts.query, keep_blank_values=True)]
    if len(keys) != len(set(keys)) or not set(keys) <= _CURSOR_QUERY_KEYS:
        return None
    return cursor


def _is_cursor_rejection(
    error: httpx.HTTPStatusError, cursor: str | None, first_request: bool
) -> bool:
    """Tell whether a Zendesk error means the caller's cursor was rejected or has expired.

    Only the first request of a resumed call uses the caller's cursor; an error on a later
    page is an ordinary error and is raised as before.

    Args:
        error: The HTTP error from Zendesk.
        cursor: The caller's cursor, or None for a call from the start.
        first_request: Whether the failing request was the first of this call.

    Returns:
        True for a 400, 404 or 422 on the first request of a resumed call.
    """
    status = error.response.status_code
    return cursor is not None and first_request and status in _CURSOR_REJECTION_STATUSES


def _invalid_cursor() -> str:
    """Return the JSON error for a cursor that is malformed, foreign or expired."""
    return json.dumps({"error": _INVALID_CURSOR_ERROR}, indent=2)


def _checked_page_cursor(url: str, expected_path: str) -> str | None:
    """Turn a next-page URL into a cursor, but only one `_cursor_url` would accept back.

    Args:
        url: The page not yet fetched.
        expected_path: The path the cursor must keep.

    Returns:
        The cursor, or None, so the server never hands out a cursor it would refuse.
    """
    cursor = _page_cursor(url)
    return cursor if _cursor_url(cursor, expected_path) is not None else None


def _call_deadline() -> float | None:
    """Return the monotonic time by which the current tool call must stop, or None for no limit.

    Only calls on /zendesk/mcp have a deadline: the route's arrival time plus
    ZENDESK_FLOW_BUDGET_SECONDS, so the token check counts against it. Call it in the tool's own
    thread and pass the value to worker threads, like the caller's auth.

    Returns:
        None in stdio mode and on /claude/mcp; otherwise the deadline.

    Raises:
        RuntimeError: HTTP mode, and the endpoint cannot be resolved (see `_current_endpoint`).
    """
    if TRANSPORT == "stdio":
        return None
    if _current_endpoint() is not Endpoint.ZENDESK_FLOW:
        return None
    request = mcp.get_context().request_context.request
    return request.scope[_REQUEST_START_SCOPE_KEY] + ZENDESK_FLOW_BUDGET_SECONDS


def _stdio_only_tool() -> Callable[[ToolFunction], ToolFunction]:
    """Register a tool in stdio mode only.

    The tools it wraps read or write the server's local filesystem, which an HTTP caller does
    not share.

    Returns:
        `mcp.tool()` in stdio mode; otherwise a decorator that leaves the function unregistered.
    """
    if TRANSPORT == "stdio":
        return mcp.tool()

    def leave_unregistered(fn: ToolFunction) -> ToolFunction:
        return fn

    return leave_unregistered


def _http_doc_remove(phrase: str, replacement: str) -> Callable[[ToolFunction], ToolFunction]:
    """Remove a phrase from a tool docstring in HTTP mode, before the tool is registered.

    Whitespace in `phrase` matches any whitespace run, so the match does not depend on the
    docstring indentation that Python 3.13+ strips at compile time.

    Args:
        phrase: Words to remove, written on one line.
        replacement: Text that replaces the phrase and the whitespace before it.

    Returns:
        A decorator that rewrites `fn.__doc__` in HTTP mode and is a no-op in stdio mode.

    Raises:
        RuntimeError: In HTTP mode, the phrase does not occur exactly once.
    """

    def decorate(fn: ToolFunction) -> ToolFunction:
        if TRANSPORT == "stdio":
            return fn
        pattern = r"\s*" + r"\s+".join(re.escape(word) for word in phrase.split())
        doc = fn.__doc__ or ""
        if len(re.findall(pattern, doc)) != 1:
            raise RuntimeError(f"{fn.__name__}: expected exactly one {phrase!r} in the docstring")
        fn.__doc__ = re.sub(pattern, replacement, doc, count=1)
        return fn

    return decorate


_HTTP_WRITE_SCOPE_NOTE = (
    "Over HTTP this tool needs an OAuth token with the `write` scope. Tokens issued through "
    "/zendesk/mcp normally have only `read`, so the tool refuses them."
)


def _http_doc_needs_write_scope() -> Callable[[ToolFunction], ToolFunction]:
    """Add the `write` scope requirement to a write tool's docstring in HTTP mode.

    The note goes in its own paragraph just before the `Args:` section. Stdio docstrings stay
    exactly as written.

    Returns:
        A decorator that rewrites `fn.__doc__` in HTTP mode and is a no-op in stdio mode.

    Raises:
        RuntimeError: In HTTP mode, the docstring does not have exactly one `Args:` line.
    """

    def decorate(fn: ToolFunction) -> ToolFunction:
        if TRANSPORT == "stdio":
            return fn
        doc = fn.__doc__ or ""
        pattern = r"\n([ \t]*)Args:"
        if len(re.findall(pattern, doc)) != 1:
            raise RuntimeError(f"{fn.__name__}: expected exactly one 'Args:' line in the docstring")
        fn.__doc__ = re.sub(
            pattern,
            lambda match: f"\n{match.group(1)}{_HTTP_WRITE_SCOPE_NOTE}\n\n{match.group(1)}Args:",
            doc,
            count=1,
        )
        return fn

    return decorate


def _doc_indent(doc: str) -> str:
    """Return the indentation of a docstring's body lines.

    Args:
        doc: The docstring.

    Returns:
        The shortest leading whitespace of the non-blank lines after the first; "" for a
        one-line docstring.
    """
    body = [line for line in doc.split("\n")[1:] if line.strip()]
    indents = [re.match(r"[ \t]*", line).group() for line in body]
    return min(indents, key=len, default="")


def _http_doc_time_budget(
    result: str = "returns what it has, with a `warning`",
) -> Callable[[ToolFunction], ToolFunction]:
    """Append the /zendesk/mcp time-budget note to a tool docstring in HTTP mode.

    Args:
        result: What the tool returns when the budget stops it.

    Returns:
        A decorator that rewrites `fn.__doc__` in HTTP mode and is a no-op in stdio mode.
    """

    def decorate(fn: ToolFunction) -> ToolFunction:
        if TRANSPORT == "stdio":
            return fn
        doc = fn.__doc__ or ""
        fn.__doc__ = doc.rstrip() + (
            f"\n\n{_doc_indent(doc)}On /zendesk/mcp (Zendesk action flows) the call stops after "
            f"{ZENDESK_FLOW_BUDGET_SECONDS:g} s and {result}."
        )
        return fn

    return decorate


_RESUME_CURSOR_ARG = (
    "cursor: HTTP only. The `next_cursor` from a previous call that the time budget cut "
    "short; omit it to start from the beginning."
)
_COMMENTS_CURSOR_ARG = (
    "cursor: HTTP only. The `next_cursor` from a previous call that the time budget cut "
    "short, or a value from get_comments_for_tickets' `unfinished`; omit it to start from the "
    "beginning."
)


def _add_doc_arg(doc: str, arg_line: str) -> str:
    """Add one argument line at the end of a docstring's `Args:` section, creating it if absent.

    Args:
        doc: The docstring.
        arg_line: The line to add, e.g. "cursor: ...", without indentation.

    Returns:
        The docstring with the line added, indented like the section's other arguments.
    """
    lines = doc.rstrip().split("\n")
    args_index = next((i for i, line in enumerate(lines) if line.strip() == "Args:"), None)
    if args_index is None:
        indent = re.match(r"[ \t]*", lines[-1]).group() if len(lines) > 1 else ""
        return "\n".join([*lines, "", f"{indent}Args:", f"{indent}    {arg_line}"])
    indent = re.match(r"[ \t]*", lines[args_index]).group()
    end = args_index + 1
    while (
        end < len(lines)
        and lines[end].strip()
        and len(lines[end]) - len(lines[end].lstrip()) > len(indent)
    ):
        end += 1
    lines.insert(end, f"{indent}    {arg_line}")
    return "\n".join(lines)


def _http_resumable(
    shape: str, arg_lines: tuple[str, ...] = (_RESUME_CURSOR_ARG,), also_stops: str = ""
) -> Callable[[ToolFunction], ToolFunction]:
    """Offer the `cursor` parameter in HTTP mode only, and document resuming there.

    In stdio mode the tool's signature is presented without `cursor`, so its input schema
    and description stay exactly as before; the parameter then always takes its default, None.
    In HTTP mode `cursor` is added to the `Args:` section and a resume note is appended.

    Args:
        shape: The keys of the cut-short response, e.g. "`comments`, `warning` and
            `next_cursor`".
        arg_lines: Lines for the `Args:` section, the `cursor:` line included. A tool whose
            docstring has no `Args:` section passes a line for every argument.
        also_stops: Another stop that also returns a cursor there, e.g. " or at the 50-page
            cap"; empty when the time budget is the only one.

    Returns:
        A decorator for a tool whose last parameter is `cursor: str | None = None`.

    Raises:
        RuntimeError: The tool has no `cursor` parameter.
    """

    def decorate(fn: ToolFunction) -> ToolFunction:
        signature = inspect.signature(fn)
        if "cursor" not in signature.parameters:
            raise RuntimeError(f"{fn.__name__}: expected a `cursor` parameter")
        if TRANSPORT == "stdio":
            kept = [param for name, param in signature.parameters.items() if name != "cursor"]
            fn.__signature__ = signature.replace(parameters=kept)
            return fn
        note = (
            f"On /zendesk/mcp (Zendesk action flows) the call stops after "
            f"{ZENDESK_FLOW_BUDGET_SECONDS:g} s{also_stops} and returns {shape} with what it "
            "has so far. A call that is not cut short returns the usual result. "
            "While a response has `next_cursor`, call again with `cursor` set to it. If an "
            "`error` says to retry with the same `cursor`, call again with that same `cursor`. "
            "Any other response without `next_cursor` ends the chain; if it has a `warning` or "
            "an `error`, the result is incomplete."
        )
        doc = fn.__doc__ or ""
        indent = _doc_indent(doc)
        for arg_line in arg_lines:
            doc = _add_doc_arg(doc, arg_line)
        fn.__doc__ = doc.rstrip() + "\n\n" + indent + note
        return fn

    return decorate


if TRANSPORT == "stdio":
    _COMMENTS_TOO_LARGE_HINT = "Use export_ticket_comments to write the thread to a local file."
else:
    _COMMENTS_TOO_LARGE_HINT = (
        "This thread is too large to return inline, and exporting to a file is not available "
        "on this server."
    )

# HTTP mode serves many callers through this one client, so its jar refuses every cookie: a
# Set-Cookie on one caller's response must never ride along on another caller's request.
_client_cookies = (
    None if TRANSPORT == "stdio" else CookieJar(DefaultCookiePolicy(allowed_domains=[]))
)
_client = httpx.Client(
    base_url=BASE_URL,
    timeout=30.0,
    headers={"Accept": "application/json"},
    cookies=_client_cookies,
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

ZendeskAuth = tuple[str, str] | httpx.Auth


class _BearerAuth(httpx.Auth):
    """Send an OAuth access token as `Authorization: Bearer`. Its repr never shows the token."""

    def __init__(self, token: str) -> None:
        self._token = token

    def __repr__(self) -> str:
        return "_BearerAuth(<redacted>)"

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._token}"
        yield request


def _caller_access_token() -> "AccessToken":
    """Return the verified OAuth token of the HTTP request being served.

    Reads the user that the auth middleware stored in the ASGI scope, which reaches the tool
    through the MCP request context. Call it in the tool's own thread: worker threads have no
    request context. The SDK's `get_access_token()` returns None here, because this server does
    not install `AuthContextMiddleware`.

    Returns:
        The caller's verified access token (its `token` and `scopes`).

    Raises:
        RuntimeError: No HTTP request, or the request has no authenticated user.
    """
    from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser

    try:
        request = mcp.get_context().request_context.request
    except ValueError:  # no MCP request in this thread
        request = None
    user = None if request is None else request.scope.get("user")
    if isinstance(user, AuthenticatedUser):
        return user.access_token
    raise RuntimeError("No authenticated caller for this request; refusing it.")


def _require_write_scope() -> None:
    """Refuse a write tool call over HTTP when the caller's token lacks the `write` scope.

    Call it first in every tool that changes Zendesk data. Stdio has no OAuth token, so it does
    nothing there; ACCESS_LEVEL still decides which write tools exist in either mode.

    Raises:
        PermissionError: HTTP mode, and the token's scopes do not include `write`.
        RuntimeError: HTTP mode, and the call has no authenticated caller.
    """
    if TRANSPORT == "stdio":
        return
    if "write" not in _caller_access_token().scopes:
        raise PermissionError(
            "This tool changes Zendesk data, and your OAuth token has no `write` scope. "
            "Sign in again through /claude/mcp to get a token with write access."
        )


def _zendesk_auth() -> ZendeskAuth:
    """Return the credentials to send with the current Zendesk request.

    Returns:
        In stdio mode, the basic-auth tuple ``("<email>/token", api_key)``. In HTTP mode, the
        caller's own OAuth token as bearer auth; HTTP mode never uses the API-token credentials.

    Raises:
        RuntimeError: HTTP mode, and the call has no authenticated caller (including a worker
            thread). No request is sent.
    """
    if TRANSPORT == "stdio":
        return (f"{EMAIL}/token", API_KEY)
    return _BearerAuth(_caller_access_token().token)


_CLIENT_TIMEOUT_SECONDS = 30.0
_DEADLINE_MIN_REQUEST_SECONDS = 1.0
_DEADLINE_MIN_WRITE_SECONDS = 5.0


def _send_get(
    path: str, params: dict | None, auth: ZendeskAuth, deadline: float | None
) -> httpx.Response:
    """Send one GET through the shared client, each timeout phase capped at the time left.

    Args:
        path: API path relative to the base URL, or an absolute `next_page` URL.
        params: Query parameters, if any.
        auth: Credentials to send.
        deadline: From `_call_deadline()`; None sends exactly as before, with the client timeout.

    Returns:
        The response.

    Raises:
        _DeadlineReached: Less than _DEADLINE_MIN_REQUEST_SECONDS are left (nothing is sent), or
            the request timed out under the capped timeout.
    """
    if deadline is None:
        return _client.get(path, params=params, auth=auth)
    remaining = deadline - time.monotonic()
    if remaining < _DEADLINE_MIN_REQUEST_SECONDS:
        raise _DeadlineReached()
    timeout = min(_CLIENT_TIMEOUT_SECONDS, remaining)
    try:
        return _client.get(path, params=params, auth=auth, timeout=timeout)
    except httpx.TimeoutException as error:
        raise _DeadlineReached() from error


def _check_time_for_write() -> None:
    """Refuse to start a write on /zendesk/mcp when too little of the budget is left.

    A write that has been sent is never cut short: its timeout stays the client default, so a
    change Zendesk applied is never reported as failed by this server.

    Raises:
        _DeadlineReached: Less than _DEADLINE_MIN_WRITE_SECONDS are left; nothing was sent.
    """
    deadline = _call_deadline()
    if deadline is not None and deadline - time.monotonic() < _DEADLINE_MIN_WRITE_SECONDS:
        raise _DeadlineReached(detail="Nothing was sent to Zendesk; no change was made.")


def _get(path: str, params: dict | None = None) -> dict:
    """Make a GET request to Zendesk API. Raises on HTTP errors.

    Resolves the caller's auth and deadline itself, so call it in the tool's own thread.
    Raises _DeadlineReached when the /zendesk/mcp time budget runs out.
    """
    resp = _send_get(path, params, _zendesk_auth(), _call_deadline())
    resp.raise_for_status()
    return resp.json()


_RETRY_MAX_ATTEMPTS = 3
_RETRY_MAX_BACKOFF_SECONDS = 30


def _get_with_retry(
    path: str,
    params: dict | None = None,
    *,
    deadline: float | None,
    auth: ZendeskAuth | None = None,
) -> dict:
    """GET with capped 429 backoff. Honors Zendesk's Retry-After header.

    Sleeps at most _RETRY_MAX_BACKOFF_SECONDS per attempt, with capped
    attempts so a sustained-429 storm fails fast instead of stalling.
    Use for loops that issue many requests against rate-limited endpoints.

    Args:
        path: API path relative to the base URL, or an absolute `next_page` URL.
        params: Query parameters, if any.
        deadline: From `_call_deadline()`, resolved in the tool's thread; None for no limit.
            A Retry-After wait that would not leave _DEADLINE_MIN_REQUEST_SECONDS stops at once.
        auth: Credentials to send. Worker threads must pass the caller's credentials,
            resolved in the calling thread; None resolves them here with `_zendesk_auth()`.

    Returns:
        The parsed JSON response.

    Raises:
        httpx.HTTPStatusError: The final attempt returned an error status (including a 429 after
            the last retry).
        _DeadlineReached: The deadline left no time for the next request or wait.
    """
    request_auth = _zendesk_auth() if auth is None else auth
    data: dict = {}
    for attempt in range(_RETRY_MAX_ATTEMPTS):
        resp = _send_get(path, params, request_auth, deadline)
        if resp.status_code == 429 and attempt < _RETRY_MAX_ATTEMPTS - 1:
            retry_after = resp.headers.get("retry-after", "5")
            try:
                wait = int(retry_after)
            except ValueError:
                wait = 5
            wait = min(max(wait, 1), _RETRY_MAX_BACKOFF_SECONDS)
            time_left = math.inf if deadline is None else deadline - time.monotonic()
            if time_left < wait + _DEADLINE_MIN_REQUEST_SECONDS:
                raise _DeadlineReached()
            time.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        break
    return data


def _post(path: str, json_body: dict) -> dict:
    """Make a POST request to Zendesk API. Raises on HTTP errors."""
    _check_time_for_write()
    resp = _client.post(path, json=json_body, auth=_zendesk_auth())
    resp.raise_for_status()
    return resp.json()


_UPLOAD_TIMEOUT_SECONDS = 120.0


def _post_upload(filename: str, content: bytes, content_type: str) -> dict:
    """Upload raw file bytes to Zendesk's uploads endpoint. Raises on HTTP errors.

    Args:
        filename: Name the attachment will have in Zendesk.
        content: The file's bytes.
        content_type: MIME type sent as the request's Content-Type.

    Returns:
        The "upload" object from the response, including its "token".
    """
    resp = _client.post(
        "/uploads.json",
        params={"filename": filename},
        content=content,
        headers={"Content-Type": content_type},
        timeout=_UPLOAD_TIMEOUT_SECONDS,
        auth=_zendesk_auth(),
    )
    resp.raise_for_status()
    return resp.json()["upload"]


def _put(path: str, json_body: dict) -> dict:
    """Make a PUT request to Zendesk API. Raises on HTTP errors."""
    _check_time_for_write()
    resp = _client.put(path, json=json_body, auth=_zendesk_auth())
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> str:
    """Make a DELETE request to Zendesk API. Raises on HTTP errors."""
    _check_time_for_write()
    resp = _client.delete(path, auth=_zendesk_auth())
    resp.raise_for_status()
    return "Deleted successfully."


# ---------------------------------------------------------------------------
# Content helpers
# ---------------------------------------------------------------------------


class _HtmlContentExtractor(HTMLParser):
    """Extract <a href> and <img src> from a comment's html_body.

    Uses a stack so nested <a> tags (rare but possible in email-quoted threads)
    don't lose links. Inline <img> sources are collected separately so callers
    can surface screenshots and email-embedded graphics distinct from links.
    """

    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self.images: list[dict] = []
        self._stack: list[tuple[str | None, list[str]]] = []

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        if tag == "a":
            self._stack.append((attrs_dict.get("href"), []))
        elif tag == "img":
            src = attrs_dict.get("src")
            if src:
                self.images.append(
                    {
                        "src": src,
                        "alt": (attrs_dict.get("alt") or "").strip(),
                    }
                )

    def handle_startendtag(self, tag, attrs):
        # Handle self-closing <img/> in XHTML-flavored HTML.
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == "a" and self._stack:
            href, parts = self._stack.pop()
            if href:
                text = "".join(parts).strip()
                self.links.append({"text": text or href, "url": href})

    def handle_data(self, data):
        if self._stack:
            self._stack[-1][1].append(data)


def _extract_html_content(html: str) -> dict:
    """Parse <a> and <img> tags from HTML, deduplicated by URL/src.

    For repeated URLs (signature + main body), keeps the variant with the
    longest non-empty anchor text, since email threads tend to put short
    boilerplate text before the actual descriptive label.

    Returns {"links": [{"text", "url"}, ...], "images": [{"src", "alt"}, ...]}.
    Empty lists on missing/invalid HTML.
    """
    if not html:
        return {"links": [], "images": []}
    parser = _HtmlContentExtractor()
    try:
        parser.feed(html)
    except Exception:  # noqa: BLE001 — malformed HTML must never break a comment fetch
        return {"links": [], "images": []}

    links: list[dict] = []
    url_to_idx: dict[str, int] = {}
    for link in parser.links:
        url = link["url"]
        if url in url_to_idx:
            existing = links[url_to_idx[url]]
            if len(link["text"]) > len(existing["text"]):
                links[url_to_idx[url]] = link
        else:
            url_to_idx[url] = len(links)
            links.append(link)

    seen_srcs: set[str] = set()
    images: list[dict] = []
    for img in parser.images:
        if img["src"] not in seen_srcs:
            seen_srcs.add(img["src"])
            images.append(img)

    return {"links": links, "images": images}


def _project_comment(c: dict) -> dict:
    """Project a raw Zendesk comment into the MCP response shape.

    Adds extracted `links`, inline `images`, and `attachments` metadata so a
    single fetch surfaces everything an extraction workflow typically needs.
    """
    content = _extract_html_content(c.get("html_body", ""))
    return {
        "id": c["id"],
        "author_id": c.get("author_id"),
        "body": c.get("body", ""),
        "plain_body": c.get("plain_body", ""),
        "links": content["links"],
        "images": content["images"],
        "attachments": [
            {
                "id": a["id"],
                "filename": a.get("file_name", ""),
                "content_type": a.get("content_type", ""),
                "size": a.get("size", 0),
                "url": a.get("content_url", ""),
            }
            for a in c.get("attachments", [])
        ],
        "public": c.get("public", True),
        "created_at": c.get("created_at", ""),
    }


def _project_ticket(t: dict) -> dict:
    """Project a raw search-result ticket into the compact list shape."""
    return {
        "id": t["id"],
        "subject": t.get("subject", ""),
        "status": t.get("status", ""),
        "priority": t.get("priority", ""),
        "created_at": t.get("created_at", ""),
        "updated_at": t.get("updated_at", ""),
        "assignee_id": t.get("assignee_id"),
        "requester_id": t.get("requester_id"),
        "tags": t.get("tags", []),
    }


def _project_article(a: dict) -> dict:
    """Project a raw search-result article into the compact list shape.
    Snippet contains a relevant content snippet in the result, with <em> tags applied to the search term and any
    HTML from the source removed.
    """
    return {
        "id": a["id"],
        "title": a.get("title", ""),
        "html_url": a.get("html_url", ""),
        "edited_at": a.get("edited_at", ""),
        "snippet": a.get("snippet", ""),
        "label_names": a.get("label_names", []),
        "user_segment_id": a.get("user_segment_id"),
        "user_segment_ids": a.get("user_segment_ids"),
    }


_SEARCH_SORT_FIELDS = {
    "ticket": ["updated_at", "created_at", "priority", "status", "ticket_type"],
    "article": ["updated_at", "created_at"],
}


def _validate_search(
    sort_by: str,
    sort_order: str,
    search_type: str,
    query: str = "",
    label_names: list[str] | None = None,
) -> dict:
    search_sort_fields = _SEARCH_SORT_FIELDS[search_type]
    if not query and search_type == "ticket":
        return {
            "error": "Must pass query. "
            "Use export_search_results or get_view_tickets for broad sweeps."
        }
    if not (query or label_names) and search_type == "article":
        return {"error": "Must pass either query or label_names, or both."}
    if sort_by:
        if sort_by not in search_sort_fields:
            return {"error": f"Invalid sort_by {sort_by!r}; must be one of {search_sort_fields}."}
        if sort_order not in ("asc", "desc"):
            return {"error": f"Invalid sort_order {sort_order!r}; must be 'asc' or 'desc'."}
    return {}


# ---------------------------------------------------------------------------
# Read-Only Tools (all access levels)
# ---------------------------------------------------------------------------


@mcp.tool()
def search_tickets(query: str, sort_by: str = "", sort_order: str = "desc") -> str:
    """Search Zendesk tickets using Zendesk search syntax.

    Returns at most one page (100 tickets). When more match, the response
    includes a `warning` with the total count — use `export_search_results`
    to page through the full result set.

    Args:
        query: Zendesk search query (type:ticket is added automatically).
        sort_by: Optional sort field: updated_at, created_at, priority,
            status, or ticket_type. Defaults to relevance ranking.
        sort_order: "asc" or "desc" (only used with sort_by; default "desc").

    Examples:
        search_tickets("status:open assignee:me")
        search_tickets("subject:refund created>2026-01-01", sort_by="created_at")
        search_tickets("priority:urgent status:open")

    See https://support.zendesk.com/hc/en-us/articles/203663226 for query syntax.
    """
    query = query.strip()
    errors = _validate_search(
        query=query, sort_by=sort_by, sort_order=sort_order, search_type="ticket"
    )
    if errors:
        return json.dumps(errors)
    params: dict = {"query": f"type:ticket {query}"}
    if sort_by:
        params |= {"sort_by": sort_by, "sort_order": sort_order}
    data = _get("/search.json", params=params)
    results = data.get("results", [])
    if not results:
        return "No tickets found."
    tickets = [_project_ticket(t) for t in results]
    out: dict = {"total_count": data.get("count", len(tickets)), "tickets": tickets}
    if data.get("next_page"):
        out["warning"] = (
            f"Showing first {len(tickets)} of {out['total_count']} matching tickets. "
            "Use export_search_results to fetch the full result set."
        )
    return json.dumps(out, indent=2)


@mcp.tool()
def count_tickets(query: str) -> str:
    """Count Zendesk tickets matching a search query, without returning them.

    Uses the search count endpoint, so it is fast and not subject to the
    search result limit. Useful for queue-size / backlog checks.

    Examples:
        count_tickets("tags:escalation status<solved")
        count_tickets("tags:data_request created>2026-01-01")

    See https://support.zendesk.com/hc/en-us/articles/203663226 for query syntax.
    """
    data = _get("/search/count.json", params={"query": f"type:ticket {query}"})
    return json.dumps({"count": data.get("count", 0)})


_EXPORT_PAGE_SIZE = 1000
_EXPORT_MAX_RESULTS = 10000
_EXPORT_MAX_PAGES = 50


_EXPORT_CURSOR_ARG = (
    "cursor: HTTP only. The `next_cursor` from a previous call that the time budget or the "
    "50-page cap cut short; pass the same `query`. When set, `max_results` is ignored: the "
    "cursor carries what is left of the first call's `max_results`."
)
# Zendesk recommends 100 per export page; 1,000 "may" slow down or time out. Used on
# /zendesk/mcp only, where one page must fit in the time budget.
_EXPORT_FLOW_PAGE_SIZE = 100
_EXPORT_CURSOR_PATTERN = re.compile(
    r"v1\.([0-9a-f]{16})\.([1-9][0-9]{0,4})\.([A-Za-z0-9+/=_-]{1,1024})"
)


def _query_fingerprint(query: str) -> str:
    """Return a short digest that ties an export cursor to its query."""
    return hashlib.sha256(query.encode()).hexdigest()[:16]


def _export_cursor(query: str, remaining: int, after_cursor: str) -> str:
    """Wrap Zendesk's `after_cursor` into an opaque resume cursor for export_search_results.

    Args:
        query: The search query the cursor belongs to.
        remaining: How many tickets the caller's `max_results` still allows.
        after_cursor: Zendesk's cursor for the next page.

    Returns:
        `v1.<query digest>.<remaining>.<after_cursor>`.
    """
    return f"v1.{_query_fingerprint(query)}.{remaining}.{after_cursor}"


def _parse_export_cursor(cursor: str, query: str) -> tuple[int, str] | None:
    """Check an export cursor before it is sent to Zendesk.

    Args:
        cursor: The caller's `cursor` argument.
        query: The caller's query; it must be the query the cursor was issued for.

    Returns:
        The remaining ticket allowance and Zendesk's `after_cursor`, or None when the cursor is
        malformed, issued for another query, or allows more than _EXPORT_MAX_RESULTS.
    """
    match = _EXPORT_CURSOR_PATTERN.fullmatch(cursor)
    if match is None or match.group(1) != _query_fingerprint(query):
        return None
    remaining = int(match.group(2))
    if remaining > _EXPORT_MAX_RESULTS:
        return None
    return remaining, match.group(3)


@mcp.tool()
@_http_resumable(
    "`returned_count`, `tickets`, `warning` and `next_cursor`",
    (_EXPORT_CURSOR_ARG,),
    also_stops=f" or at the {_EXPORT_MAX_PAGES}-page cap",
)
def export_search_results(query: str, max_results: int = 1000, cursor: str | None = None) -> str:
    """Export the full set of tickets matching a search query.

    Unlike `search_tickets` (a single page of at most 100 tickets, and the
    underlying search API stops at 1,000), this uses the cursor-based export
    endpoint: complete, duplicate-free result sets — the right tool for audits
    and reporting sweeps. Results come back roughly newest-first by
    created_at; no custom sorting, and strict order is not guaranteed.

    Retries 429s with Retry-After backoff. Stops after `max_results` tickets
    (capped at 10,000) or 50 requests, whichever comes first; the response
    carries a `warning` naming the reason whenever more tickets match.

    Args:
        query: Zendesk search query (ticket type is applied automatically).
        max_results: Stop after this many tickets (default 1000, max 10000).

    Examples:
        export_search_results("tags:escalation created>2026-01-01")
        export_search_results("status:solved solved>2026-06-01", max_results=5000)

    See https://support.zendesk.com/hc/en-us/articles/203663226 for query syntax.
    """
    start = _export_start(query, max_results, cursor)
    if start is None:
        return _invalid_cursor()
    limit, params = start
    deadline = _call_deadline()
    page_size = _EXPORT_PAGE_SIZE if deadline is None else _EXPORT_FLOW_PAGE_SIZE
    tickets: list[dict] = []
    pages = 0
    while True:
        params["page[size]"] = min(page_size, limit - len(tickets))
        try:
            data = _get_with_retry("/search/export.json", params=params, deadline=deadline)
        except _DeadlineReached as stop:
            if pages == 0:
                return _no_progress_error(stop, cursor)
            return _export_stopped(query, tickets, limit, params["page[after]"], str(stop))
        except httpx.HTTPStatusError as error:
            if not _is_cursor_rejection(error, cursor, first_request=pages == 0):
                raise
            return _invalid_cursor()
        pages += 1
        tickets.extend(_project_ticket(t) for t in data.get("results", []))
        meta = data.get("meta", {})
        truncated_by = _export_stop_reason(meta, len(tickets), limit, pages, cursor)
        if truncated_by is None:
            params["page[after]"] = meta["after_cursor"]
            continue
        if truncated_by == _EXPORT_PAGE_CAP and deadline is not None:
            # Small flow pages: let the flow resume past the cap; each call stays bounded.
            reason = f"Stopped by {_EXPORT_PAGE_CAP}."
            return _export_stopped(query, tickets, limit, meta["after_cursor"], reason)
        return _export_result(tickets, limit, truncated_by, cursor)


_EXPORT_PAGE_CAP = f"the request cap ({_EXPORT_MAX_PAGES} pages)"


def _export_start(query: str, max_results: int, cursor: str | None) -> tuple[int, dict] | None:
    """Work out an export call's ticket allowance and first request parameters.

    Args:
        query: The search query.
        max_results: The caller's `max_results`; ignored when `cursor` is set.
        cursor: The caller's cursor, or None to start from the beginning.

    Returns:
        The allowance and the parameters, or None when the cursor fails `_parse_export_cursor`.
    """
    params: dict = {"query": query, "filter[type]": "ticket"}
    if cursor is None:
        return max(1, min(max_results, _EXPORT_MAX_RESULTS)), params
    parsed = _parse_export_cursor(cursor, query)
    if parsed is None:
        return None
    limit, params["page[after]"] = parsed
    return limit, params


def _export_stop_reason(
    meta: dict, ticket_count: int, limit: int, pages: int, cursor: str | None
) -> str | None:
    """Decide whether an export call goes on after a page, and if not, why it stops.

    Args:
        meta: The page's `meta` from Zendesk.
        ticket_count: Tickets gathered so far in this call.
        limit: The call's ticket allowance.
        pages: Pages fetched so far in this call.
        cursor: The caller's cursor, if this call is a resume.

    Returns:
        None to fetch the next page; "" when the result set is complete; otherwise the reason
        the call stops short, as used in the `warning`.
    """
    if not meta.get("has_more"):
        return ""
    if ticket_count >= limit:
        return f"max_results ({limit})" if cursor is None else "the max_results limit"
    if not meta.get("after_cursor"):
        return "a missing cursor in the Zendesk response"
    if pages >= _EXPORT_MAX_PAGES:
        return _EXPORT_PAGE_CAP
    return None


def _export_result(tickets: list[dict], limit: int, truncated_by: str, cursor: str | None) -> str:
    """Build export_search_results' answer when the call ends without a resume cursor.

    Args:
        tickets: Tickets gathered in this call.
        limit: The call's ticket allowance.
        truncated_by: Why the call stopped short, or "" when the result set is complete.
        cursor: The caller's cursor, if this call is a resume.

    Returns:
        "No tickets found." for an empty first call; otherwise JSON with the tickets and, when
        cut short, a `warning`. A resumed call always answers JSON, even when empty.
    """
    if not tickets and not truncated_by and cursor is None:
        return "No tickets found."
    out: dict = {"returned_count": min(len(tickets), limit), "tickets": tickets[:limit]}
    if truncated_by:
        out["warning"] = (
            f"Result set incomplete — stopped by {truncated_by}; more tickets match. "
            "Raise max_results or narrow the query."
        )
    return json.dumps(out, indent=2)


def _export_stopped(
    query: str, tickets: list[dict], limit: int, after_cursor: str, reason: str
) -> str:
    """Build export_search_results' resumable answer on /zendesk/mcp.

    Used when the time budget, or the page cap with flow-sized pages, stops a call after at
    least one page.

    Args:
        query: The search query.
        tickets: Tickets gathered in this call.
        limit: The ticket allowance for this call.
        after_cursor: Zendesk's cursor for the page that was not fetched.
        reason: Why the call stopped.

    Returns:
        JSON with the tickets so far and a `warning`, plus a `next_cursor` carrying the rest of
        the allowance when the cursor passes `_parse_export_cursor`.
    """
    out: dict = {"returned_count": len(tickets), "tickets": tickets}
    next_cursor = _export_cursor(query, limit - len(tickets), after_cursor)
    if _parse_export_cursor(next_cursor, query) is None:
        out["warning"] = f"Result set incomplete. {reason} {_CURSOR_NOT_RESUMABLE}"
        return json.dumps(out, indent=2)
    out["warning"] = (
        f"Result set incomplete. {reason} Call again with the same query and "
        "`cursor` = `next_cursor`."
    )
    out["next_cursor"] = next_cursor
    return json.dumps(out, indent=2)


@mcp.tool()
def get_ticket(ticket_id: int) -> str:
    """Get full details for a single Zendesk ticket by ID."""
    data = _get(f"/tickets/{ticket_id}.json")
    t = data["ticket"]
    result = {
        "id": t["id"],
        "subject": t.get("subject", ""),
        "description": t.get("description", ""),
        "status": t.get("status", ""),
        "priority": t.get("priority", ""),
        "type": t.get("type", ""),
        "created_at": t.get("created_at", ""),
        "updated_at": t.get("updated_at", ""),
        "assignee_id": t.get("assignee_id"),
        "requester_id": t.get("requester_id"),
        "group_id": t.get("group_id"),
        "organization_id": t.get("organization_id"),
        "tags": t.get("tags", []),
        "custom_fields": t.get("custom_fields", []),
    }
    return json.dumps(result, indent=2)


@mcp.tool()
@_http_doc_remove("; use `export_ticket_comments` to write those to a local file.", ".")
@_http_resumable("`comments`, `warning` and `next_cursor`", (_COMMENTS_CURSOR_ARG,))
def get_ticket_comments(ticket_id: int, cursor: str | None = None) -> str:
    """Get all comments (replies) on a Zendesk ticket, in chronological order.

    Follows Zendesk's `next_page` pagination so long threads (>100 comments)
    are returned in full. Threads over ~1 MB of projected JSON are refused with
    an `error` instead of being returned inline; use `export_ticket_comments`
    to write those to a local file.

    Each comment includes:
    - `links`: anchor URLs extracted from html_body (recovers triage URLs that
      the markdown body flattens to plain text).
    - `images`: inline <img src> URLs and alt text.
    - `attachments`: filename, url, content_type, size for each attachment.

    To fetch attachment content, use `fetch_attachment(url)`.

    Args:
        ticket_id: The Zendesk ticket ID.
    """
    start_url = None
    if cursor is not None:
        start_url = _cursor_url(cursor, f"/tickets/{ticket_id}/comments.json")
        if start_url is None:
            return _invalid_cursor()
    try:
        comments = _fetch_comments(
            ticket_id,
            max_bytes=_COMMENTS_INLINE_MAX_BYTES,
            deadline=_call_deadline(),
            start_url=start_url,
        )
    except _CommentThreadTooLarge as exc:
        return json.dumps(exc.to_error(), indent=2)
    except _DeadlineReached as stop:
        if not stop.progressed:
            return _no_progress_error(stop, cursor)
        return _cut_short("comments", stop.partial, stop, stop.next_cursor)
    except _CursorRejected:
        return _invalid_cursor()
    return json.dumps(comments, indent=2)


@_stdio_only_tool()
def export_ticket_comments(ticket_id: int, output_path: str) -> str:
    """Write all comments on a ticket to a local JSON file instead of returning them.

    Use this for threads too large for `get_ticket_comments` (e.g. a bot
    ticket that has accumulated thousands of notices), then process the file
    with a script rather than reading it into context. The file holds a
    compact JSON array in the same per-comment shape as `get_ticket_comments`.
    An existing file at `output_path` is overwritten; parent directories are
    created. Retries 429s with Retry-After backoff.

    Args:
        ticket_id: The Zendesk ticket ID.
        output_path: Absolute path of the JSON file to write (`~` is expanded).

    Returns:
        JSON with `path`, `comment_count`, and `bytes` written, or an `error`.
    """
    path = Path(output_path).expanduser()
    if not path.is_absolute():
        return json.dumps({"error": "output_path must be an absolute path."})
    comments = _fetch_comments(ticket_id, max_bytes=None, deadline=None)  # stdio only
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(comments))
    result = {"path": str(path), "comment_count": len(comments), "bytes": path.stat().st_size}
    return json.dumps(result, indent=2)


@mcp.tool()
def get_user(user_id: int) -> str:
    """Look up a Zendesk user by their ID. Useful after finding a requester_id or assignee_id."""
    data = _get(f"/users/{user_id}.json")
    u = data["user"]
    result = {
        "id": u["id"],
        "name": u.get("name", ""),
        "email": u.get("email", ""),
        "role": u.get("role", ""),
        "organization_id": u.get("organization_id"),
        "tags": u.get("tags", []),
        "created_at": u.get("created_at", ""),
        "last_login_at": u.get("last_login_at", ""),
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def search_users(query: str) -> str:
    """Search Zendesk users by name or email.

    Examples:
        search_users("jane@example.com")
        search_users("John Smith")
    """
    data = _get("/users/search.json", params={"query": query})
    users = []
    for u in data.get("users", []):
        users.append(
            {
                "id": u["id"],
                "name": u.get("name", ""),
                "email": u.get("email", ""),
                "role": u.get("role", ""),
                "organization_id": u.get("organization_id"),
            }
        )
    return json.dumps(users, indent=2)


@mcp.tool()
def get_view_tickets(view_id: int) -> str:
    """Execute a saved Zendesk view and return its tickets.

    Use this to check queues like "Unassigned tickets" or "My open tickets".
    You need the numeric view ID (visible in the Zendesk URL when viewing a view).
    """
    data = _get(f"/views/{view_id}/tickets.json")
    tickets = []
    for t in data.get("tickets", []):
        tickets.append(
            {
                "id": t["id"],
                "subject": t.get("subject", ""),
                "status": t.get("status", ""),
                "priority": t.get("priority", ""),
                "requester_id": t.get("requester_id"),
                "assignee_id": t.get("assignee_id"),
                "updated_at": t.get("updated_at", ""),
            }
        )
    return json.dumps(tickets, indent=2)


@mcp.tool()
def list_views() -> str:
    """List all available Zendesk views with their IDs and names."""
    data = _get("/views.json")
    views = []
    for v in data.get("views", []):
        views.append(
            {
                "id": v["id"],
                "title": v.get("title", ""),
                "active": v.get("active", True),
            }
        )
    return json.dumps(views, indent=2)


@mcp.tool()
# Main's docstring has no `Args:` section, so the HTTP-only one lists `ticket_id` too.
@_http_resumable(
    "`audits`, `warning` and `next_cursor`",
    ("ticket_id: The Zendesk ticket ID.", _RESUME_CURSOR_ARG),
)
def get_ticket_audits(ticket_id: int, cursor: str | None = None) -> str:
    """Get the audit log for a ticket — field changes, notifications, etc.

    Follows Zendesk's `next_page` pagination so busy tickets return their
    full audit history.

    Returns events in chronological order. Comment and VoiceComment events
    are skipped (use get_ticket_comments for those). Change and Notification
    events are fully projected; any other event types are surfaced as
    `{"type": etype}` stubs so callers can see something happened without
    having to refetch the raw audits endpoint.
    """
    audits_path = f"/tickets/{ticket_id}/audits.json"
    url = audits_path if cursor is None else _cursor_url(cursor, audits_path)
    if url is None:
        return _invalid_cursor()
    first_url = url
    audits: list = []
    while url:
        try:
            data = _get(url)
        except _DeadlineReached as stop:
            if url == first_url:
                return _no_progress_error(stop, cursor)
            return _cut_short("audits", audits, stop, _checked_page_cursor(url, audits_path))
        except httpx.HTTPStatusError as error:
            if not _is_cursor_rejection(error, cursor, first_request=url == first_url):
                raise
            return _invalid_cursor()
        audits.extend(_project_audits(data))
        url = data.get("next_page")
    return json.dumps(audits, indent=2)


def _project_audits(data: dict) -> list[dict]:
    """Project one page of ticket audits for get_ticket_audits.

    Args:
        data: A Zendesk audits page.

    Returns:
        The page's audits that have events left after skipping comments, each projected.
    """
    audits: list[dict] = []
    for a in data.get("audits", []):
        events = []
        for e in a.get("events", []):
            etype = e.get("type")
            if etype == "Change":
                events.append(
                    {
                        "type": "Change",
                        "field": e.get("field_name"),
                        "previous_value": e.get("previous_value"),
                        "value": e.get("value"),
                    }
                )
            elif etype == "Notification":
                events.append(
                    {
                        "type": "Notification",
                        "recipients": e.get("recipients", []),
                        "subject": e.get("subject", ""),
                    }
                )
            elif etype in ("Comment", "VoiceComment"):
                continue
            elif etype:
                events.append({"type": etype})
        if events:
            audits.append(
                {
                    "id": a["id"],
                    "author_id": a.get("author_id"),
                    "created_at": a.get("created_at", ""),
                    "events": events,
                }
            )
    return audits


# Inline comment responses are capped well below the MCP client's 16 MB
# per-message limit; JSON-RPC string escaping inflates them further in transit.
_COMMENTS_INLINE_MAX_BYTES = 1_000_000
_BULK_COMMENTS_MAX_BYTES = 4_000_000


class _CursorRejected(Exception):
    """Zendesk rejected the first request of a call resumed from a caller's cursor."""


class _CommentThreadTooLarge(Exception):
    """Raised when a ticket's comment thread exceeds the inline byte budget."""

    def __init__(self, ticket_id: int, fetched: int, total: int | None):
        super().__init__(f"Ticket {ticket_id} comment thread exceeds the inline size limit.")
        self.ticket_id = ticket_id
        self.fetched = fetched
        self.total = total

    def to_error(self) -> dict:
        """Build the structured error returned to the MCP caller.

        Returns:
            Dict with `error`, `ticket_id`, comment counts, and a `hint`.
        """
        return {
            "error": str(self),
            "ticket_id": self.ticket_id,
            "comments_fetched_before_stop": self.fetched,
            "total_comments": self.total,
            "hint": _COMMENTS_TOO_LARGE_HINT,
        }


def _fetch_comments(
    ticket_id: int,
    max_bytes: int | None,
    *,
    deadline: float | None,
    auth: ZendeskAuth | None = None,
    start_url: str | None = None,
) -> list[dict]:
    """Fetch all comments for one ticket, paginated, with 429 backoff.

    Args:
        ticket_id: The Zendesk ticket ID.
        max_bytes: Stop after the page that pushes the projected JSON past this
            many bytes. None fetches the whole thread.
        deadline: From `_call_deadline()`, resolved in the tool's thread; None for no limit.
        start_url: A checked cursor (see `_cursor_url`) to resume from; None starts at page 1.
        auth: Credentials to send. Pass the caller's when running in a worker thread;
            None resolves them with `_zendesk_auth()` in the current thread.

    Returns:
        Projected comments in chronological order.

    Raises:
        _CommentThreadTooLarge: The thread exceeds `max_bytes`.
        _DeadlineReached: The deadline stopped the fetch; `partial` holds the comments so far
            and `next_cursor` the page to resume from.
        _CursorRejected: Zendesk answered 400, 404 or 422 to the `start_url` request.
    """
    comments: list[dict] = []
    total_bytes = 0
    thread_path = f"/tickets/{ticket_id}/comments.json"
    url: str | None = start_url or thread_path
    pages = 0
    while url:
        try:
            data = _get_with_retry(url, auth=auth, deadline=deadline)
        except _DeadlineReached as stop:
            raise _DeadlineReached(
                partial=comments,
                next_cursor=_checked_page_cursor(url, thread_path),
                progressed=pages > 0,
            ) from stop
        except httpx.HTTPStatusError as error:
            if _is_cursor_rejection(error, start_url, first_request=pages == 0):
                raise _CursorRejected() from error
            raise
        pages += 1
        for raw_comment in data.get("comments", []):
            projected = _project_comment(raw_comment)
            comments.append(projected)
            total_bytes += len(json.dumps(projected))
        if max_bytes is not None and total_bytes > max_bytes:
            raise _CommentThreadTooLarge(ticket_id, len(comments), data.get("count"))
        url = data.get("next_page")
    return comments


@mcp.tool()
@_http_doc_remove("(fetch it with `export_ticket_comments`)", "")
@_http_doc_time_budget(
    "returns the threads fetched so far, a `warning`, and `unfinished`, which maps each "
    "incomplete ticket_id to a cursor; finish each with get_ticket_comments(ticket_id, "
    "cursor=...) and append the result. A thread that cannot be resumed is listed in `errors` "
    "instead. An answer over the size limit has a `warning` and no `unfinished`: retry with "
    "fewer ticket_ids. If a resumed ticket was deleted meanwhile, the resume reports an invalid "
    "cursor"
)
def get_comments_for_tickets(ticket_ids: list[int], max_concurrency: int = 8) -> str:
    """Bulk-fetch comments for many tickets in parallel.

    Speeds up per-ticket analysis (e.g. queue-level triage) across an entire
    view: fetches up to `max_concurrency` ticket comment threads concurrently
    and returns a dict keyed by ticket_id, with the same per-comment shape as
    `get_ticket_comments` (links, images, attachments included). Follows
    pagination per ticket and retries 429s with Retry-After backoff.

    A ticket whose thread is over ~1 MB lands in `errors` (fetch it with
    `export_ticket_comments`). If the combined response would exceed ~4 MB,
    only an `error` and per-ticket `comment_counts` are returned; retry with
    fewer ticket_ids.

    Args:
        ticket_ids: List of ticket IDs to fetch (max 200).
        max_concurrency: Maximum simultaneous Zendesk requests (1–20, default 8).
    """
    if len(ticket_ids) > 200:
        return json.dumps({"error": "Maximum 200 tickets per bulk fetch."})

    workers = max(1, min(max_concurrency, 20))

    results: dict[str, list] = {}
    errors: dict[str, str] = {}
    unfinished: dict[str, str] = {}
    # Resolved here: contextvars do not carry into executor threads by default.
    caller_auth = _zendesk_auth()
    caller_deadline = _call_deadline()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {
            ex.submit(
                _fetch_comments,
                tid,
                _COMMENTS_INLINE_MAX_BYTES,
                auth=caller_auth,
                deadline=caller_deadline,
            ): tid
            for tid in ticket_ids
        }
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                results[str(tid)] = fut.result()
            except _DeadlineReached as stop:
                if stop.next_cursor is None:
                    errors[str(tid)] = f"Incomplete. {stop} {_CURSOR_NOT_RESUMABLE}"
                else:
                    results[str(tid)] = stop.partial
                    unfinished[str(tid)] = stop.next_cursor
            except Exception as exc:  # noqa: BLE001 — one failed ticket must not kill the batch
                errors[str(tid)] = str(exc)

    resume = _bulk_resume_fields(unfinished, len(ticket_ids))
    out: dict = {"comments_by_ticket": results}
    if errors:
        out["errors"] = errors
    out |= resume
    serialized = json.dumps(out, indent=2)
    if len(serialized) <= _BULK_COMMENTS_MAX_BYTES:
        return serialized
    oversized: dict = {
        "error": "Combined comments exceed the inline size limit; request fewer ticket_ids.",
        "comment_counts": {tid: len(comments) for tid, comments in results.items()},
    }
    if errors:
        oversized["errors"] = errors
    if unfinished:
        # No comments are returned here, so a cursor would skip the ones fetched before the stop.
        oversized["warning"] = "The time budget also ran out before every thread was fetched."
    return json.dumps(oversized, indent=2)


def _bulk_resume_fields(unfinished: dict[str, str], ticket_count: int) -> dict:
    """Build the resume part of a get_comments_for_tickets answer.

    Args:
        unfinished: Cursor per ticket_id whose thread the time budget cut short.
        ticket_count: How many tickets the call asked for.

    Returns:
        `unfinished` and a `warning` that says how to finish each thread; empty when none.
    """
    if not unfinished:
        return {}
    warning = (
        f"{len(unfinished)} of {ticket_count} threads are incomplete: the time budget ran out. "
        "For each ticket_id in `unfinished`, call get_ticket_comments with that ticket_id and "
        "`cursor` set to its value, and append the result."
    )
    return {"unfinished": unfinished, "warning": warning}


_IMAGE_FORMAT_ALLOWLIST = {"png", "jpeg", "gif", "webp"}


_ATTACHMENT_TIMEOUT_SECONDS = 60.0


@mcp.tool()
@_http_doc_time_budget("returns `kind: stopped_by_time_budget` instead of the content")
def fetch_attachment(url: str, max_size_mb: int = 10):
    """Fetch the contents of a Zendesk attachment by URL.

    For images, returns inline image content the model can view directly.
    For text-like content (text/*, JSON, CSV, XML), returns decoded text.
    For other binary types, returns metadata only — no base64 dump, since
    that bloats context without giving the model anything actionable.

    Refuses URLs not on the configured Zendesk subdomain (exact hostname
    match), to prevent the auth-bearing client from being pointed at
    arbitrary hosts. Streams the body and enforces `max_size_mb` early so
    a malicious or oversized response can't exhaust memory. Redirects are
    not followed; if Zendesk redirects to a CDN, the redirect target is
    surfaced as a `{kind: "redirect"}` response for the caller to inspect.

    Args:
        url: Attachment URL (typically from get_ticket_comments → attachments[*].url).
        max_size_mb: Refuse to fetch attachments larger than this (default 10).
    """
    expected_host = f"{SUBDOMAIN}.zendesk.com"
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != expected_host:
        return json.dumps(
            {
                "error": f"URL is not on the configured Zendesk subdomain ({expected_host}).",
            }
        )

    max_bytes = max_size_mb * 1024 * 1024
    deadline = _call_deadline()
    timeout = _ATTACHMENT_TIMEOUT_SECONDS
    if deadline is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout < _DEADLINE_MIN_REQUEST_SECONDS:
            return _attachment_stopped(0)
    try:
        return _stream_attachment(url, max_bytes, timeout, deadline)
    except httpx.TimeoutException:
        if deadline is None:
            raise
        return _attachment_stopped(None)


def _attachment_stopped(bytes_read: int | None) -> str:
    """Build fetch_attachment's answer when the /zendesk/mcp time budget runs out.

    Args:
        bytes_read: Bytes received before the stop, or None when unknown.

    Returns:
        JSON with `kind` "stopped_by_time_budget" and a `warning`.
    """
    return json.dumps(
        {
            "kind": "stopped_by_time_budget",
            "bytes_read": bytes_read,
            "warning": str(_DeadlineReached()),
        },
        indent=2,
    )


def _stream_attachment(
    url: str, max_bytes: int, timeout: float, deadline: float | None
) -> str | Image:
    """Stream one attachment for fetch_attachment; the URL guard has already passed."""
    with httpx.stream(
        "GET",
        url,
        auth=_zendesk_auth(),
        timeout=timeout,
        follow_redirects=False,
    ) as resp:
        if resp.is_redirect:
            return json.dumps(
                {
                    "kind": "redirect",
                    "status_code": resp.status_code,
                    "location": resp.headers.get("location", ""),
                    "message": (
                        "Attachment is served from a redirected location (likely a CDN). "
                        "Redirects are not followed automatically. Inspect the location "
                        "and, if you trust it, fetch it via a separate channel."
                    ),
                }
            )

        resp.raise_for_status()

        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        declared_length = resp.headers.get("content-length")
        if declared_length is not None:
            try:
                declared_int = int(declared_length)
            except ValueError:
                declared_int = 0
            if declared_int > max_bytes:
                return json.dumps(
                    {
                        "kind": "too_large",
                        "size_bytes": declared_int,
                        "max_bytes": max_bytes,
                        "content_type": content_type,
                    }
                )

        buf = bytearray()
        too_large = False
        for chunk in resp.iter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                too_large = True
                break
            if deadline is not None and time.monotonic() > deadline:
                return _attachment_stopped(len(buf))

        if too_large:
            return json.dumps(
                {
                    "kind": "too_large",
                    "size_bytes": len(buf),
                    "max_bytes": max_bytes,
                    "content_type": content_type,
                }
            )

        content = bytes(buf)

    return _attachment_content(content, content_type)


def _attachment_content(content: bytes, content_type: str) -> str | Image:
    """Turn a fetched attachment into fetch_attachment's answer (image, text or metadata).

    Args:
        content: The attachment bytes, already within the size cap.
        content_type: The response's media type, lowercased, without parameters.

    Returns:
        An Image for images, decoded text for text-like types, otherwise JSON metadata.
    """
    if content_type.startswith("image/"):
        fmt_raw = content_type.removeprefix("image/").split("+")[0]
        if fmt_raw == "jpg":
            fmt_raw = "jpeg"
        fmt = fmt_raw if fmt_raw in _IMAGE_FORMAT_ALLOWLIST else "png"
        return Image(data=content, format=fmt)

    text_like = (
        content_type.startswith("text/")
        or "json" in content_type
        or "xml" in content_type
        or "csv" in content_type
    )
    if text_like:
        return content.decode("utf-8", errors="replace")

    return json.dumps(
        {
            "kind": "binary",
            "content_type": content_type,
            "size_bytes": len(content),
            "message": "Binary content not returned. Download externally if needed.",
            "preview_b64_first_64": base64.b64encode(content[:64]).decode("ascii"),
        }
    )


_ARTICLES_MAX_RESULTS = 1000  # hard API limit; don't raise past 1000
_ARTICLES_PER_PAGE = 100
_ARTICLES_MAX_PAGES = math.ceil(_ARTICLES_MAX_RESULTS / _ARTICLES_PER_PAGE)


def _article_search_params(
    query: str, label_names: list[str], sort_by: str, sort_order: str
) -> dict:
    """Build the first-page query parameters for search_articles.

    Args:
        query: The search text.
        label_names: Cleaned label names; may be empty.
        sort_by: Sort field, or "" for relevance.
        sort_order: "asc" or "desc"; used only with `sort_by`.

    Returns:
        The parameters for page 1.
    """
    params: dict = {"query": query, "page": 1, "per_page": _ARTICLES_PER_PAGE}
    if label_names:
        params["label_names"] = ",".join(label_names)
    if sort_by:
        params |= {"sort_by": sort_by, "sort_order": sort_order}
    return params


@mcp.tool()
@_http_doc_time_budget()
def search_articles(
    query: str = "",
    label_names: list[str] | None = None,
    sort_by: str = "",
    sort_order: str = "desc",
    max_results: int = 50,
) -> str:
    """Search the Help Center for articles, including ones restricted to a user segment.

    Returns a default number of 50 article summaries, up to a maximum of 1000 results.
    Plain text only — ticket operators like status:open match as literal words. Drafts are not indexed.

    Retries 429s with Retry-After backoff. Stops after max_results articles, the endpoint's
    1000-article ceiling, or 10 requests, whichever comes first.

    Args:
        query: The search text to be matched. Example: "goal segment". Supply query, label_names, or both.
        label_names: A list of strings consisting of label names. Articles matching any of the labels are
            returned. Case-insensitive; labels may contain spaces. None by default.
        sort_by: Optional sort field: "created_at" or "updated_at". Defaults to relevance ranking.
        sort_order: "asc" or "desc" (only used with sort_by; default "desc").
        max_results: Maximum number of articles to be returned. Default is 50; maximum is 1000.

    Examples:
        search_articles("goal segment")
        search_articles("email", label_names=["Best Practices", "groups"])
        search_articles("", label_names=["Retargeting"])
        search_articles("upsync", sort_by="updated_at", max_results=10)

    Returns:
        A json object that contains a list of article summaries, max_results reconciled against the
        1000-result maximum, and the total number of articles that matched the query. (Note: the
        endpoint caps total_count at 1000.) Includes a warning when max_results or the endpoint's
        1000-article ceiling cut the result set short. user_segment_id and user_segment_ids
        indicate if an article is restricted to a user segment; public if both are null. User
        segment field use depends on Guide plan. Use list_user_segments() to resolve segment IDs to names.
        Use get_article() for article body, section_id, and content_tag_ids.

    See https://developer.zendesk.com/api-reference/help_center/help-center-api/articles/ for the endpoint spec.
    """
    query = query.strip()
    label_names = [label.strip() for label in label_names or [] if label.strip()]
    errors = _validate_search(
        query=query,
        label_names=label_names,
        sort_by=sort_by,
        sort_order=sort_order,
        search_type="article",
    )
    if errors:
        return json.dumps(errors)
    params = _article_search_params(query, label_names, sort_by, sort_order)
    articles: list[dict] = []
    count = 0
    page_count = 0
    limit = max(1, min(max_results, _ARTICLES_MAX_RESULTS))
    warning: str = ""
    deadline = _call_deadline()
    budget_stop: _DeadlineReached | None = None
    while page_count < _ARTICLES_MAX_PAGES:
        try:
            data = _get_with_retry(
                "/help_center/articles/search.json", params=params, deadline=deadline
            )
        except _DeadlineReached as stop:
            budget_stop = stop
            warning = (
                f"Showing {min(len(articles), limit)} articles; the list may be incomplete. {stop}"
            )
            break
        count = count or data.get("count", 0)
        articles.extend([_project_article(a) for a in data.get("results", [])])
        next_page = data.get("next_page")
        page_count += 1
        if len(articles) == _ARTICLES_MAX_RESULTS == limit:
            warning = (
                f"Showing first {_ARTICLES_MAX_RESULTS} matching articles. Ranked by relevance by "
                "default. There may be additional matches, but the endpoint can only gather "
                f"{_ARTICLES_MAX_RESULTS} articles total."
            )
            break
        if (len(articles) > limit) or (len(articles) == limit and next_page):
            warning = (
                f"Showing first {limit} of {count or 'unknown'} matching articles. "
                f"Ranked by relevance by default. Raise max_results (up to {_ARTICLES_MAX_RESULTS}) to get more."
            )
            break
        if not next_page:
            break
        params["page"] += 1

    if not articles and budget_stop is None:
        return "No articles found."
    out: dict = {
        "articles": articles[:limit],
        "max_results": limit,
        "total_count": count or len(articles),
    }
    if warning:
        out["warning"] = warning
    return json.dumps(out, indent=2)


@mcp.tool()
def get_article(article_id: int) -> str:
    """Get full details for a single Help Center article by ID.

    user_segment_id and user_segment_ids indicate if an article is restricted to a user segment;
    public if both are null. User segment field use depends on Guide plan. Use list_user_segments() to resolve
    segment IDs to names. Use list_categories_and_sections() to find the article's category and section,
    and list_content_tags() to resolve content_tag_ids to names.
    """
    data = _get(f"/help_center/articles/{article_id}.json")
    a = data["article"]
    result = {
        "id": a["id"],
        "html_url": a.get("html_url", ""),
        "title": a.get("title", ""),
        "body": a.get("body", ""),
        "edited_at": a.get("edited_at", ""),
        "updated_at": a.get("updated_at", ""),
        "user_segment_id": a.get("user_segment_id"),
        "user_segment_ids": a.get("user_segment_ids"),
        "draft": a.get("draft", False),
        "section_id": a.get("section_id"),
        "label_names": a.get("label_names", []),
        "content_tag_ids": a.get("content_tag_ids", []),
    }
    return json.dumps(result, indent=2)


_USER_SEGMENTS_PER_PAGE = 100


@mcp.tool()
def list_user_segments() -> str:
    """List all Zendesk user segments available on the current Guide plan.

    Returns:
        A JSON object of user segments; 1 page, 100 results max. Each user segment has fields for ID, name, user type,
        tags, and or_tags. To view an article, a user must have all tags listed in the tags field, and, when
        or_tags is set, at least one tag from it. Includes a warning when the 100-result max cuts the result
        set short.

    See https://developer.zendesk.com/api-reference/help_center/help-center-api/user_segments/ for the endpoint spec.
    """
    data = _get("/help_center/user_segments/applicable.json", {"per_page": _USER_SEGMENTS_PER_PAGE})
    count = data.get("count", 0)
    user_segments = []
    for user_segment in data.get("user_segments", []):
        user_segments.append(
            {
                "id": user_segment["id"],
                "name": user_segment.get("name", ""),
                "user_type": user_segment.get("user_type", ""),
                "tags": user_segment.get("tags", []),
                "or_tags": user_segment.get("or_tags", []),
            }
        )
    out: dict = {"user_segments": user_segments}
    if data.get("next_page"):
        out["warning"] = (
            f"Showing first {_USER_SEGMENTS_PER_PAGE} of {count or 'unknown'} user segments."
        )
    return json.dumps(out, indent=2)


_CATEGORIES_PER_PAGE = 100
_SECTIONS_PER_PAGE = 100


@mcp.tool()
def list_categories_and_sections() -> str:
    """List all Help Center sections, grouped by the category they belong to.

    Returns:
        A JSON object of returned category and section counts, and categories keyed by category ID; 1 page,
        100 categories and 100 sections max. Each category has fields for name, html_url, and sections, and
        each section has fields for ID, name, and html_url. Includes a warning when either 100-result max
        cuts the result set short, and when a section is omitted because a category is missing from the response.

    See https://developer.zendesk.com/api-reference/help_center/help-center-api/categories/ and
    https://developer.zendesk.com/api-reference/help_center/help-center-api/sections/ for the endpoint specs.
    """
    categories_data = _get("/help_center/categories.json", {"per_page": _CATEGORIES_PER_PAGE})
    categories_and_sections: dict = {}
    for category in categories_data.get("categories", []):
        categories_and_sections[category["id"]] = {
            "name": category.get("name", ""),
            "html_url": category.get("html_url", ""),
            "sections": [],
        }

    if not categories_and_sections:
        return "No categories found."

    sections_data = _get("/help_center/sections.json", {"per_page": _SECTIONS_PER_PAGE})
    returned_section_count = 0
    omitted_section_count = 0
    for section in sections_data.get("sections", []):
        parent_category = categories_and_sections.get(section.get("category_id"))
        if parent_category is None:
            omitted_section_count += 1
            continue
        parent_category["sections"].append(
            {
                "id": section["id"],
                "name": section.get("name", ""),
                "html_url": section.get("html_url", ""),
            }
        )
        returned_section_count += 1

    out: dict = {
        "returned_category_count": len(categories_and_sections),
        "returned_section_count": returned_section_count,
        "categories_and_sections": categories_and_sections,
    }
    warnings = []
    if categories_data.get("next_page"):
        category_count = categories_data.get("count") or "unknown"
        warnings.append(f"Showing first {_CATEGORIES_PER_PAGE} of {category_count} categories.")
    if sections_data.get("next_page"):
        section_count = sections_data.get("count") or "unknown"
        warnings.append(f"Showing first {_SECTIONS_PER_PAGE} of {section_count} sections.")
    if omitted_section_count:
        warnings.append(
            f"Omitted {omitted_section_count} sections whose category is not in this response."
        )
    if warnings:
        out["warning"] = " ".join(warnings)
    return json.dumps(out, indent=2)


_CONTENT_TAGS_PAGE_SIZE = 30  # hard API limit; requests above 30 are rejected with a 400
_CONTENT_TAGS_MAX_PAGES = 34  # ~1000 tags, mirroring the article cap
_CONTENT_TAGS_MAX_RESULTS = _CONTENT_TAGS_PAGE_SIZE * _CONTENT_TAGS_MAX_PAGES


@mcp.tool()
@_http_doc_time_budget()
def list_content_tags() -> str:
    """List all Help Center content tags.

    The endpoint is cursor-paginated at 30 per page, so this walks the cursor to gather the full set.
    Retries 429s with Retry-After backoff. Stops after 34 requests (1020 content tags).

    Returns:
        A JSON object of content tags. Each content tag has fields for ID and name. Includes a warning
        naming the reason whenever the result set was cut short.

    See https://developer.zendesk.com/api-reference/help_center/help-center-api/content_tags/ for the endpoint spec.
    """
    params: dict = {"page[size]": _CONTENT_TAGS_PAGE_SIZE}
    content_tags = []
    truncated_by: str | None = None
    pages = 0
    deadline = _call_deadline()
    budget_stop: _DeadlineReached | None = None
    while True:
        try:
            data = _get_with_retry("/guide/content_tags", params=params, deadline=deadline)
        except _DeadlineReached as stop:
            budget_stop = stop
            break
        pages += 1
        for content_tag in data.get("records", []):
            content_tags.append(
                {
                    "id": content_tag["id"],
                    "name": content_tag.get("name", ""),
                }
            )
        meta = data.get("meta", {})
        if not meta.get("has_more"):
            break
        if not meta.get("after_cursor"):
            truncated_by = "a missing cursor in the Zendesk response"
            break
        if pages >= _CONTENT_TAGS_MAX_PAGES:
            truncated_by = (
                f"the request cap ({_CONTENT_TAGS_MAX_PAGES} pages, "
                f"{_CONTENT_TAGS_MAX_RESULTS} content tags)"
            )
            break
        params["page[after]"] = meta["after_cursor"]

    if not content_tags and budget_stop is None:
        return "No content tags found."

    out: dict = {
        "returned_count": len(content_tags),
        "content_tags": content_tags,
    }
    if budget_stop is not None:
        out["warning"] = f"The list may be incomplete. {budget_stop}"
    elif truncated_by:
        out["warning"] = (
            f"Result set incomplete — stopped by {truncated_by}; more content tags exist."
        )
    return json.dumps(out, indent=2)


# ---------------------------------------------------------------------------
# Ticket Management Tools (management + admin)
# ---------------------------------------------------------------------------

_ATTACHMENT_MAX_BYTES = 50 * 1024 * 1024  # Zendesk's per-file attachment limit


def _resolve_attachment_paths(file_paths: list[str]) -> list[Path]:
    """Expand and validate local files before any of them are uploaded.

    Args:
        file_paths: Paths as given by the caller; "~" is expanded.

    Returns:
        Resolved paths, in the order given.

    Raises:
        ValueError: If no paths are given, or any path is missing, not a regular
            file, or larger than the attachment limit.
    """
    if not file_paths:
        raise ValueError("file_paths must contain at least one path.")
    resolved: list[Path] = []
    problems: list[str] = []
    for raw_path in file_paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            problems.append(f"{raw_path}: not found or not a regular file")
            continue
        size = path.stat().st_size
        if size > _ATTACHMENT_MAX_BYTES:
            problems.append(f"{raw_path}: {size / 1024 / 1024:.1f} MB exceeds the 50 MB limit")
            continue
        resolved.append(path)
    if problems:
        raise ValueError("Nothing was uploaded. " + "; ".join(problems))
    return resolved


if ACCESS_LEVEL in ("management", "admin"):

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def create_ticket(
        subject: str,
        body: str,
        requester_email: str | None = None,
        priority: str | None = None,
        ticket_type: str | None = None,
        tags: list[str] | None = None,
        assignee_id: int | None = None,
        group_id: int | None = None,
    ) -> str:
        """Create a new Zendesk ticket.

        Args:
            subject: Ticket subject line.
            body: Initial comment body (the ticket description).
            requester_email: Email of the requester. If omitted, you are the requester.
            priority: One of "low", "normal", "high", "urgent". Optional.
            ticket_type: One of "problem", "incident", "question", "task". Optional.
            tags: List of tags to apply. Optional.
            assignee_id: User ID to assign the ticket to. Optional.
            group_id: Group ID to assign the ticket to. Optional.
        """
        _require_write_scope()
        ticket: dict = {
            "subject": subject,
            "comment": {"body": body},
        }
        if requester_email:
            ticket["requester"] = {"email": requester_email}
        if priority:
            ticket["priority"] = priority
        if ticket_type:
            ticket["type"] = ticket_type
        if tags:
            ticket["tags"] = tags
        if assignee_id:
            ticket["assignee_id"] = assignee_id
        if group_id:
            ticket["group_id"] = group_id

        data = _post("/tickets.json", {"ticket": ticket})
        t = data["ticket"]
        return json.dumps({"id": t["id"], "subject": t["subject"], "status": t["status"]}, indent=2)

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def update_ticket(
        ticket_id: int,
        status: str | None = None,
        priority: str | None = None,
        ticket_type: str | None = None,
        subject: str | None = None,
        assignee_id: int | None = None,
        group_id: int | None = None,
        tags: list[str] | None = None,
        custom_fields: list[dict] | None = None,
    ) -> str:
        """Update fields on an existing Zendesk ticket.

        Only provided fields are changed — omitted fields are left as-is.

        Args:
            ticket_id: The ticket to update.
            status: One of "new", "open", "pending", "hold", "solved", "closed".
            priority: One of "low", "normal", "high", "urgent".
            ticket_type: One of "problem", "incident", "question", "task".
            subject: New subject line.
            assignee_id: User ID to reassign to.
            group_id: Group ID to reassign to.
            tags: Replace all tags with this list.
            custom_fields: List of {"id": field_id, "value": value} dicts.
        """
        _require_write_scope()
        ticket: dict = {}
        if status:
            ticket["status"] = status
        if priority:
            ticket["priority"] = priority
        if ticket_type:
            ticket["type"] = ticket_type
        if subject:
            ticket["subject"] = subject
        if assignee_id:
            ticket["assignee_id"] = assignee_id
        if group_id:
            ticket["group_id"] = group_id
        if tags is not None:
            ticket["tags"] = tags
        if custom_fields is not None:
            ticket["custom_fields"] = custom_fields

        if not ticket:
            return "No fields to update."

        data = _put(f"/tickets/{ticket_id}.json", {"ticket": ticket})
        t = data["ticket"]
        return json.dumps(
            {
                "id": t["id"],
                "status": t["status"],
                "priority": t.get("priority"),
                "assignee_id": t.get("assignee_id"),
                "tags": t.get("tags", []),
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def add_ticket_comment(
        ticket_id: int,
        body: str,
        public: bool = True,
    ) -> str:
        """Add a comment to a Zendesk ticket.

        Args:
            ticket_id: The ticket to comment on.
            body: The comment text.
            public: True for a public reply (visible to requester), False for an internal note.
        """
        _require_write_scope()
        data = _put(
            f"/tickets/{ticket_id}.json",
            {
                "ticket": {
                    "comment": {"body": body, "public": public},
                }
            },
        )
        return json.dumps(
            {
                "ticket_id": data["ticket"]["id"],
                "status": data["ticket"]["status"],
                "comment_added": True,
                "public": public,
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def add_ticket_tags(ticket_id: int, tags: list[str]) -> str:
        """Add tags to a ticket without removing existing ones.

        Args:
            ticket_id: The ticket to tag.
            tags: Tags to add.
        """
        _require_write_scope()
        existing = _get(f"/tickets/{ticket_id}.json")
        current_tags = existing["ticket"].get("tags", [])
        merged = list(set(current_tags + tags))
        data = _put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": merged}})
        return json.dumps({"id": ticket_id, "tags": data["ticket"].get("tags", [])}, indent=2)

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def remove_ticket_tags(ticket_id: int, tags: list[str]) -> str:
        """Remove specific tags from a ticket.

        Args:
            ticket_id: The ticket to update.
            tags: Tags to remove.
        """
        _require_write_scope()
        existing = _get(f"/tickets/{ticket_id}.json")
        current_tags = existing["ticket"].get("tags", [])
        updated = [t for t in current_tags if t not in tags]
        data = _put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": updated}})
        return json.dumps({"id": ticket_id, "tags": data["ticket"].get("tags", [])}, indent=2)

    @_stdio_only_tool()
    @_http_doc_needs_write_scope()
    def attach_file_to_ticket(
        ticket_id: int,
        file_paths: list[str],
        comment: str,
        public: bool = False,
    ) -> str:
        """Attach one or more local files to a ticket in a single new comment.

        Reads each file from the local machine running this server, uploads it to
        Zendesk, and adds one comment to the ticket carrying all the attachments.
        Defaults to an internal note; set public=True only when the requester should
        receive the files. All paths are checked before anything is uploaded, so a bad
        path fails without posting a partial comment. Files over 50 MB are rejected
        (Zendesk's attachment limit).

        Args:
            ticket_id: The ticket to attach the files to.
            file_paths: Local file paths to attach. Absolute paths or paths starting
                with "~" are safest; relative paths resolve against the server's working directory.
            comment: Comment text posted with the attachments. Zendesk requires a
                comment to carry attachments, so this cannot be empty.
            public: True for a public reply (visible to requester), False for an internal note.
        """
        _require_write_scope()
        if not comment.strip():
            raise ValueError("comment must not be empty; Zendesk attaches files via a comment.")
        files = _resolve_attachment_paths(file_paths)

        tokens: list[str] = []
        attachments: list[dict] = []
        for path in files:
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            content = path.read_bytes()
            upload = _post_upload(path.name, content, content_type)
            tokens.append(upload["token"])
            attachments.append(
                {
                    "file_name": path.name,
                    "size_bytes": len(content),
                    "content_type": content_type,
                }
            )

        data = _put(
            f"/tickets/{ticket_id}.json",
            {"ticket": {"comment": {"body": comment, "public": public, "uploads": tokens}}},
        )
        return json.dumps(
            {
                "ticket_id": data["ticket"]["id"],
                "status": data["ticket"]["status"],
                "public": public,
                "attachments": attachments,
            },
            indent=2,
        )


# ---------------------------------------------------------------------------
# Full Admin Tools (admin only)
# ---------------------------------------------------------------------------

if ACCESS_LEVEL == "admin":

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def merge_tickets(target_ticket_id: int, source_ticket_ids: list[int]) -> str:
        """Merge one or more tickets into a target ticket.

        The source tickets will be closed and their comments merged into the target.

        Args:
            target_ticket_id: The ticket that will remain open and receive merged content.
            source_ticket_ids: Ticket IDs to merge into the target.
        """
        _require_write_scope()
        data = _post(
            f"/tickets/{target_ticket_id}/merge.json",
            {
                "ids": source_ticket_ids,
            },
        )
        return json.dumps(
            {
                "target_ticket_id": target_ticket_id,
                "merged_ticket_ids": source_ticket_ids,
                "status": data.get("job_status", {}).get("status", "queued"),
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def create_user(
        name: str,
        email: str,
        role: str = "end-user",
        organization_id: int | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Create a new Zendesk user.

        Args:
            name: Full name.
            email: Email address.
            role: One of "end-user", "agent", "admin". Defaults to "end-user".
            organization_id: Organization to assign the user to. Optional.
            tags: Tags to apply to the user. Optional.
        """
        _require_write_scope()
        user: dict = {"name": name, "email": email, "role": role}
        if organization_id:
            user["organization_id"] = organization_id
        if tags:
            user["tags"] = tags

        data = _post("/users.json", {"user": user})
        u = data["user"]
        return json.dumps(
            {
                "id": u["id"],
                "name": u["name"],
                "email": u["email"],
                "role": u["role"],
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def update_user(
        user_id: int,
        name: str | None = None,
        email: str | None = None,
        role: str | None = None,
        organization_id: int | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Update a Zendesk user.

        Args:
            user_id: The user to update.
            name: New name. Optional.
            email: New email. Optional.
            role: New role ("end-user", "agent", "admin"). Optional.
            organization_id: New organization. Optional.
            tags: Replace all tags with this list. Optional.
        """
        _require_write_scope()
        user: dict = {}
        if name:
            user["name"] = name
        if email:
            user["email"] = email
        if role:
            user["role"] = role
        if organization_id:
            user["organization_id"] = organization_id
        if tags is not None:
            user["tags"] = tags

        if not user:
            return "No fields to update."

        data = _put(f"/users/{user_id}.json", {"user": user})
        u = data["user"]
        return json.dumps(
            {
                "id": u["id"],
                "name": u["name"],
                "email": u["email"],
                "role": u["role"],
                "tags": u.get("tags", []),
            },
            indent=2,
        )

    @mcp.tool()
    def get_organization(organization_id: int) -> str:
        """Get details for a Zendesk organization by ID."""
        data = _get(f"/organizations/{organization_id}.json")
        o = data["organization"]
        return json.dumps(
            {
                "id": o["id"],
                "name": o.get("name", ""),
                "domain_names": o.get("domain_names", []),
                "tags": o.get("tags", []),
                "group_id": o.get("group_id"),
                "created_at": o.get("created_at", ""),
            },
            indent=2,
        )

    @mcp.tool()
    def search_organizations(query: str) -> str:
        """Search Zendesk organizations by name.

        Examples:
            search_organizations("Acme Corp")
        """
        data = _get("/organizations/search.json", params={"name": query})
        orgs = []
        for o in data.get("organizations", []):
            orgs.append(
                {
                    "id": o["id"],
                    "name": o.get("name", ""),
                    "domain_names": o.get("domain_names", []),
                    "tags": o.get("tags", []),
                }
            )
        return json.dumps(orgs, indent=2)

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def create_organization(
        name: str,
        domain_names: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Create a new Zendesk organization.

        Args:
            name: Organization name.
            domain_names: Associated domain names (e.g. ["acme.com"]). Optional.
            tags: Tags to apply. Optional.
        """
        _require_write_scope()
        org: dict = {"name": name}
        if domain_names:
            org["domain_names"] = domain_names
        if tags:
            org["tags"] = tags

        data = _post("/organizations.json", {"organization": org})
        o = data["organization"]
        return json.dumps(
            {
                "id": o["id"],
                "name": o["name"],
                "domain_names": o.get("domain_names", []),
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def update_organization(
        organization_id: int,
        name: str | None = None,
        domain_names: list[str] | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Update a Zendesk organization.

        Args:
            organization_id: The organization to update.
            name: New name. Optional.
            domain_names: New domain names list. Optional.
            tags: Replace all tags with this list. Optional.
        """
        _require_write_scope()
        org: dict = {}
        if name:
            org["name"] = name
        if domain_names is not None:
            org["domain_names"] = domain_names
        if tags is not None:
            org["tags"] = tags

        if not org:
            return "No fields to update."

        data = _put(f"/organizations/{organization_id}.json", {"organization": org})
        o = data["organization"]
        return json.dumps(
            {
                "id": o["id"],
                "name": o["name"],
                "domain_names": o.get("domain_names", []),
                "tags": o.get("tags", []),
            },
            indent=2,
        )

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def delete_ticket(ticket_id: int) -> str:
        """Permanently delete a Zendesk ticket. This cannot be undone.

        Args:
            ticket_id: The ticket to delete.
        """
        _require_write_scope()
        return _delete(f"/tickets/{ticket_id}.json")

    @mcp.tool()
    @_http_doc_needs_write_scope()
    def bulk_update_tickets(
        ticket_ids: list[int],
        status: str | None = None,
        priority: str | None = None,
        assignee_id: int | None = None,
        group_id: int | None = None,
        tags: list[str] | None = None,
    ) -> str:
        """Update multiple tickets at once.

        Args:
            ticket_ids: List of ticket IDs to update (max 100).
            status: New status for all tickets. Optional.
            priority: New priority for all tickets. Optional.
            assignee_id: Reassign all tickets to this user. Optional.
            group_id: Reassign all tickets to this group. Optional.
            tags: Replace tags on all tickets. Optional.
        """
        _require_write_scope()
        if len(ticket_ids) > 100:
            return "Error: Maximum 100 tickets per bulk update."

        ticket: dict = {}
        if status:
            ticket["status"] = status
        if priority:
            ticket["priority"] = priority
        if assignee_id:
            ticket["assignee_id"] = assignee_id
        if group_id:
            ticket["group_id"] = group_id
        if tags is not None:
            ticket["tags"] = tags

        if not ticket:
            return "No fields to update."

        ids_str = ",".join(str(i) for i in ticket_ids)
        data = _put(f"/tickets/update_many.json?ids={ids_str}", {"ticket": ticket})
        return json.dumps(
            {
                "updated_count": len(ticket_ids),
                "job_status": data.get("job_status", {}).get("status", "queued"),
            },
            indent=2,
        )


if TRANSPORT == "streamable-http":
    import hashlib
    from datetime import datetime, timezone

    import anyio
    from mcp.server.auth.handlers.metadata import MetadataHandler
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from mcp.server.auth.provider import AccessToken, TokenVerifier
    from mcp.server.auth.routes import (
        build_resource_metadata_url,
        cors_middleware,
        create_protected_resource_routes,
    )
    from mcp.shared.auth import OAuthMetadata
    from pydantic import AnyHttpUrl, Field
    from starlette.applications import Starlette
    from starlette.authentication import AuthenticationError
    from starlette.middleware.authentication import AuthenticationMiddleware
    from starlette.requests import HTTPConnection, Request
    from starlette.responses import JSONResponse, RedirectResponse, Response
    from starlette.routing import Route
    from starlette.types import ASGIApp, Receive, Scope, Send

    class _EndpointTag:
        """ASGI wrapper that records which endpoint a request arrived on, and when.

        It sits outside the token check, so the arrival time it records for `_call_deadline`
        includes that check.

        A class, not a function: Starlette treats a plain function endpoint as a request handler.
        """

        def __init__(self, app: ASGIApp, endpoint: Endpoint) -> None:
            self.app = app
            self.endpoint = endpoint

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            tags = {_ENDPOINT_SCOPE_KEY: self.endpoint, _REQUEST_START_SCOPE_KEY: time.monotonic()}
            await self.app({**scope, **tags}, receive, send)

    # Every token must carry `read`; the SDK answers 403 insufficient_scope otherwise.
    _REQUIRED_SCOPES = ["read"]
    _TOKEN_CACHE_SECONDS = 60
    _TOKEN_CACHE_MAX_ENTRIES = 1024
    _TOKEN_CHECK_TIMEOUT_SECONDS = 10.0

    class ZendeskAccessToken(AccessToken):
        """An AccessToken that also records the Zendesk user the token acts as.

        The token is left out of repr, str, rich output and `model_dump`, so formatting or
        dumping this object never reveals it; read it as the `token` attribute only.
        """

        token: str = Field(repr=False, exclude=True)
        user_id: int

    class ZendeskUnavailable(AuthenticationError):
        """Zendesk could not check a token (429, 5xx, timeout or network error)."""

    def _parse_expires_at(value: object) -> int | None:
        """Turn Zendesk's `expires_at` (e.g. `2026-11-11T20:52:43Z`) into epoch seconds.

        Args:
            value: The `expires_at` field of a `tokens/current` answer.

        Returns:
            Epoch seconds (UTC), or None when the value is missing or not in that format.
        """
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            return None
        return int(parsed.replace(tzinfo=timezone.utc).timestamp())

    class ZendeskTokenVerifier(TokenVerifier):
        """Check bearer tokens with Zendesk `GET /api/v2/oauth/tokens/current`.

        Valid results are cached by SHA-256 of the token for up to `_TOKEN_CACHE_SECONDS`, never
        past the token's `expires_at`, with at most `_TOKEN_CACHE_MAX_ENTRIES` entries. Invalid
        results are not cached. Tokens never appear in logs, reprs or exception text.
        """

        def __init__(self) -> None:
            self._cache: dict[str, tuple[ZendeskAccessToken, float]] = {}

        async def verify_token(self, token: str) -> AccessToken | None:
            """Return the token's details if Zendesk accepts it.

            Args:
                token: The bearer token from the request.

            Returns:
                A ZendeskAccessToken, or None when the token is not valid: Zendesk answered
                something other than 200, 429 or 5xx, or the token belongs to another OAuth client.

            Raises:
                ZendeskUnavailable: Zendesk answered 429 or 5xx, or could not be reached. The
                    client gets 503, not 401, so it keeps its token.
            """
            cache_key = hashlib.sha256(token.encode()).hexdigest()
            now = time.time()
            cached = self._cache.get(cache_key)
            if cached is not None and cached[1] > now:
                return cached[0]
            self._cache.pop(cache_key, None)
            access_token = await anyio.to_thread.run_sync(_fetch_token_info, token)
            if access_token is None:
                return None
            cache_until = now + _TOKEN_CACHE_SECONDS
            if access_token.expires_at is not None:
                cache_until = min(cache_until, access_token.expires_at)
            self._store(cache_key, access_token, cache_until)
            return access_token

        def _store(self, cache_key: str, access_token: ZendeskAccessToken, until: float) -> None:
            """Cache one result, dropping the oldest entries when full.

            Args:
                cache_key: SHA-256 hex digest of the token.
                access_token: The verified token details.
                until: Epoch seconds after which the entry is stale.
            """
            while len(self._cache) >= _TOKEN_CACHE_MAX_ENTRIES:
                self._cache.pop(next(iter(self._cache)))
            self._cache[cache_key] = (access_token, until)

    def _fetch_token_info(token: str) -> "ZendeskAccessToken | None":
        """Ask Zendesk about one token, through the shared client (runs in a worker thread).

        Args:
            token: The bearer token to check.

        Returns:
            The token's details on a 200 for the pinned OAuth client. None on any other status
            except 429 and 5xx, or when the token belongs to another OAuth client.

        Raises:
            ZendeskUnavailable: 429, 5xx, a malformed 200 body, or a transport error.
        """
        try:
            response = _client.get(
                "/oauth/tokens/current.json",
                auth=_BearerAuth(token),
                timeout=_TOKEN_CHECK_TIMEOUT_SECONDS,
            )
        except httpx.HTTPError as error:
            raise ZendeskUnavailable(
                f"Zendesk token check failed: {type(error).__name__}"
            ) from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise ZendeskUnavailable(f"Zendesk token check returned {status}")
        if status != 200:
            return None
        try:
            info = response.json()["token"]
            if str(info["client_id"]) != OAUTH_CLIENT_ID:
                return None  # a token from another OAuth client on this Zendesk instance
            return ZendeskAccessToken(
                token=token,
                client_id=str(info["client_id"]),
                scopes=[str(scope) for scope in info["scopes"]],
                expires_at=_parse_expires_at(info.get("expires_at")),
                user_id=int(info["user_id"]),
            )
        except (ValueError, KeyError, TypeError):
            raise ZendeskUnavailable("Zendesk token check returned an unreadable body") from None

    def _token_check_unavailable(conn: HTTPConnection, error: AuthenticationError) -> JSONResponse:
        """Answer 503 when Zendesk could not check the token, so the client keeps it and retries.

        Args:
            conn: The request (unused).
            error: The error from the verifier; its text names no token.

        Returns:
            A 503 JSON response with Retry-After.
        """
        return JSONResponse(
            {"error": "temporarily_unavailable", "error_description": str(error)},
            status_code=503,
            headers={"Retry-After": "5"},
        )

    _token_verifier = ZendeskTokenVerifier()

    # Clients request the scopes a resource advertises: Zendesk flows get read-only tokens.
    _SCOPES_SUPPORTED = {ZENDESK_FLOW_PATH: ["read"], CLAUDE_CODE_PATH: ["read", "write"]}
    _ROOT_SCOPES_SUPPORTED = ["read"]
    _ZENDESK_OAUTH_URL = f"https://{SUBDOMAIN}.zendesk.com/oauth"
    _AUTHORIZE_PATH = "/oauth/authorize"

    def _require_caller_auth(endpoint_app: ASGIApp, path: str) -> ASGIApp:
        """Wrap one endpoint so it answers 401 without a Zendesk token that has every required scope.

        Args:
            endpoint_app: The MCP transport endpoint. `_EndpointTag` wraps the result, so the
                arrival time it records includes this token check.
            path: The endpoint path; its metadata URL goes in the 401 `WWW-Authenticate` header.

        Returns:
            The endpoint behind AuthenticationMiddleware and RequireAuthMiddleware.
        """
        metadata_url = build_resource_metadata_url(AnyHttpUrl(RESOURCE_BASE_URL + path))
        return AuthenticationMiddleware(
            RequireAuthMiddleware(endpoint_app, _REQUIRED_SCOPES, metadata_url),
            backend=BearerAuthBackend(_token_verifier),
            on_error=_token_check_unavailable,
        )

    @mcp.custom_route("/health", methods=["GET"])
    async def health(request: Request) -> JSONResponse:
        """Liveness check for load balancers; makes no Zendesk call.

        Args:
            request: The incoming request (unused).

        Returns:
            JSON `{"status": "ok"}` with status 200.
        """
        return JSONResponse({"status": "ok"})

    def _build_http_app() -> Starlette:
        """Build the ASGI app with the MCP endpoint served at both endpoint paths.

        Returns:
            The FastMCP Starlette app, with its single MCP route replaced by two routes that
            share the same transport endpoint and session manager. Each route is wrapped by
            `_require_caller_auth`, so it answers 401 without a valid Zendesk token, and then
            by `_EndpointTag` on the outside, so the recorded arrival time includes the token
            check. The OAuth discovery routes and the `/oauth/authorize` redirect from
            `_oauth_metadata_routes` are appended.

        Raises:
            RuntimeError: The SDK app does not have exactly one MCP route to replace.
        """
        http_app = mcp.streamable_http_app()
        mcp_path = mcp.settings.streamable_http_path
        matches = [
            index
            for index, route in enumerate(http_app.router.routes)
            if isinstance(route, Route) and route.path == mcp_path
        ]
        if len(matches) != 1:
            raise RuntimeError(f"mcp SDK app layout changed: expected one MCP route at {mcp_path}")
        index = matches[0]
        transport_endpoint = http_app.router.routes[index].endpoint
        http_app.router.routes[index : index + 1] = [
            Route(
                ZENDESK_FLOW_PATH,
                endpoint=_EndpointTag(
                    _require_caller_auth(transport_endpoint, ZENDESK_FLOW_PATH),
                    Endpoint.ZENDESK_FLOW,
                ),
            ),
            Route(
                CLAUDE_CODE_PATH,
                endpoint=_EndpointTag(
                    _require_caller_auth(transport_endpoint, CLAUDE_CODE_PATH),
                    Endpoint.CLAUDE_CODE,
                ),
            ),
        ]
        http_app.router.routes.extend(_oauth_metadata_routes())
        return http_app

    def _oauth_metadata_routes() -> list[Route]:
        """Build the OAuth discovery routes and the authorize redirect.

        Returns:
            Protected resource metadata for each endpoint path and the root, authorization
            server metadata naming Zendesk's token endpoint, and the authorize redirect.
        """
        base = AnyHttpUrl(RESOURCE_BASE_URL)
        routes: list[Route] = []
        for path, scopes in _SCOPES_SUPPORTED.items():
            routes += create_protected_resource_routes(
                AnyHttpUrl(RESOURCE_BASE_URL + path), [base], scopes_supported=scopes
            )
        routes += create_protected_resource_routes(
            base, [base], scopes_supported=_ROOT_SCOPES_SUPPORTED
        )
        metadata = OAuthMetadata(
            issuer=base,
            authorization_endpoint=AnyHttpUrl(RESOURCE_BASE_URL + _AUTHORIZE_PATH),
            token_endpoint=AnyHttpUrl(f"{_ZENDESK_OAUTH_URL}/tokens"),
            scopes_supported=["read", "write"],
            grant_types_supported=["authorization_code", "refresh_token"],
            token_endpoint_auth_methods_supported=["client_secret_post"],
            code_challenge_methods_supported=["S256"],
        )
        metadata_endpoint = cors_middleware(MetadataHandler(metadata).handle, ["GET", "OPTIONS"])
        routes.append(
            Route(
                "/.well-known/oauth-authorization-server",
                endpoint=metadata_endpoint,
                methods=["GET", "OPTIONS"],
            )
        )
        routes.append(Route(_AUTHORIZE_PATH, endpoint=_authorize_redirect, methods=["GET"]))
        return routes

    async def _authorize_redirect(request: Request) -> Response:
        """Send the browser to Zendesk's authorize page without the `resource` parameter.

        Zendesk's authorize endpoint rejects RFC 8707 `resource` for ordinary OAuth clients; its
        token endpoint accepts it. The target host is fixed from ZENDESK_SUBDOMAIN, and every
        other percent-encoded query parameter passes through unchanged (raw non-ASCII bytes come
        out percent-encoded), so Zendesk still checks `redirect_uri`, PKCE and `state`.

        Args:
            request: The GET request from the user's browser.

        Returns:
            A 302 redirect to `https://<subdomain>.zendesk.com/oauth/authorizations/new`.
        """
        query = request.scope.get("query_string", b"").decode("latin-1")
        kept: list[str] = []
        for segment in query.split("&"):
            name = unquote_plus(segment.split("=", 1)[0])
            if segment and name != "resource":
                kept.append(segment)
        target = f"{_ZENDESK_OAUTH_URL}/authorizations/new"
        if kept:
            target += "?" + "&".join(kept)
        return RedirectResponse(target, status_code=302)

    app = _build_http_app()


if __name__ == "__main__":
    if TRANSPORT == "stdio":
        mcp.run(transport="stdio")
    else:
        import uvicorn

        # Pass the app object: an import string would import this module a second time.
        uvicorn.run(app, host=mcp.settings.host, port=mcp.settings.port)
