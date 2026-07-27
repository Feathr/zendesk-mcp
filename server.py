"""Zendesk MCP Server — ticket triage, management, and admin.

Access levels controlled by ACCESS_LEVEL env var:
    readonly   — search/read/count/export tickets, users, views, orgs, audits, bulk read, attachments (12 tools)
    management — readonly + create/update tickets, comments, tags (17 tools)
    admin      — management + user/org CRUD, merge, bulk ops, delete (26 tools)
"""

import base64
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Image

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
EMAIL = os.environ["ZENDESK_EMAIL"]
API_KEY = os.environ["ZENDESK_API_KEY"]
ACCESS_LEVEL = os.environ.get("ACCESS_LEVEL", "readonly").lower()

if ACCESS_LEVEL not in ("readonly", "management", "admin"):
    raise ValueError(
        f"Invalid ACCESS_LEVEL: {ACCESS_LEVEL!r}. Must be readonly, management, or admin."
    )

BASE_URL = f"https://{SUBDOMAIN}.zendesk.com/api/v2"

mcp = FastMCP("zendesk")

_client = httpx.Client(
    base_url=BASE_URL,
    auth=(f"{EMAIL}/token", API_KEY),
    timeout=30.0,
    headers={"Accept": "application/json"},
)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _get(path: str, params: dict | None = None) -> dict:
    """Make a GET request to Zendesk API. Raises on HTTP errors."""
    resp = _client.get(path, params=params)
    resp.raise_for_status()
    return resp.json()


def _post(path: str, json_body: dict) -> dict:
    """Make a POST request to Zendesk API. Raises on HTTP errors."""
    resp = _client.post(path, json=json_body)
    resp.raise_for_status()
    return resp.json()


def _put(path: str, json_body: dict) -> dict:
    """Make a PUT request to Zendesk API. Raises on HTTP errors."""
    resp = _client.put(path, json=json_body)
    resp.raise_for_status()
    return resp.json()


def _delete(path: str) -> str:
    """Make a DELETE request to Zendesk API. Raises on HTTP errors."""
    resp = _client.delete(path)
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


# ---------------------------------------------------------------------------
# Read-Only Tools (all access levels)
# ---------------------------------------------------------------------------

_SEARCH_SORT_FIELDS = ("updated_at", "created_at", "priority", "status", "ticket_type")


@mcp.tool()
def search_tickets(query: str, sort_by: str = "", sort_order: str = "") -> str:
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
    params: dict = {"query": f"type:ticket {query}"}
    if sort_by:
        if sort_by not in _SEARCH_SORT_FIELDS:
            return json.dumps(
                {"error": f"Invalid sort_by {sort_by!r}; must be one of {_SEARCH_SORT_FIELDS}."}
            )
        params["sort_by"] = sort_by
        params["sort_order"] = "asc" if sort_order == "asc" else "desc"
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


@mcp.tool()
def export_search_results(query: str, max_results: int = 1000) -> str:
    """Export the full set of tickets matching a search query.

    Unlike `search_tickets` (one page, 1,000-result hard cap), this uses the
    cursor-based export endpoint: complete, duplicate-free result sets — the
    right tool for audits and reporting sweeps. Results come back roughly
    newest-first by created_at; no custom sorting, and strict order is not
    guaranteed. Zendesk rate-limits this endpoint to 100 requests/minute;
    each request fetches up to 1,000 tickets.

    Args:
        query: Zendesk search query (ticket type is applied automatically).
        max_results: Stop after this many tickets (default 1000, max 10000).

    Examples:
        export_search_results("tags:escalation created>2026-01-01")
        export_search_results("status:solved solved>2026-06-01", max_results=5000)

    See https://support.zendesk.com/hc/en-us/articles/203663226 for query syntax.
    """
    limit = max(1, min(max_results, _EXPORT_MAX_RESULTS))
    tickets: list[dict] = []
    params: dict = {
        "query": query,
        "filter[type]": "ticket",
        "page[size]": min(_EXPORT_PAGE_SIZE, limit),
    }
    while True:
        data = _get("/search/export.json", params=params)
        tickets.extend(_project_ticket(t) for t in data.get("results", []))
        meta = data.get("meta", {})
        has_more = bool(meta.get("has_more")) and bool(meta.get("after_cursor"))
        if not has_more or len(tickets) >= limit:
            break
        params["page[after]"] = meta["after_cursor"]

    if not tickets:
        return "No tickets found."
    out: dict = {"count": min(len(tickets), limit), "tickets": tickets[:limit]}
    if has_more or len(tickets) > limit:
        out["warning"] = (
            f"Stopped at max_results={limit}; more tickets match. "
            "Raise max_results or narrow the query."
        )
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
def get_ticket_comments(ticket_id: int) -> str:
    """Get all comments (replies) on a Zendesk ticket, in chronological order.

    Follows Zendesk's `next_page` pagination so long threads (>100 comments)
    are returned in full.

    Each comment includes:
    - `links`: anchor URLs extracted from html_body (recovers triage URLs that
      the markdown body flattens to plain text).
    - `images`: inline <img src> URLs and alt text.
    - `attachments`: filename, url, content_type, size for each attachment.

    To fetch attachment content, use `fetch_attachment(url)`.
    """
    comments: list = []
    url: str | None = f"/tickets/{ticket_id}/comments.json"
    while url:
        data = _get(url)
        comments.extend(_project_comment(c) for c in data.get("comments", []))
        url = data.get("next_page")
    return json.dumps(comments, indent=2)


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
def get_ticket_audits(ticket_id: int) -> str:
    """Get the audit log for a ticket — field changes, notifications, etc.

    Follows Zendesk's `next_page` pagination so busy tickets return their
    full audit history.

    Returns events in chronological order. Comment and VoiceComment events
    are skipped (use get_ticket_comments for those). Change and Notification
    events are fully projected; any other event types are surfaced as
    `{"type": etype}` stubs so callers can see something happened without
    having to refetch the raw audits endpoint.
    """
    audits: list = []
    url: str | None = f"/tickets/{ticket_id}/audits.json"
    while url:
        data = _get(url)
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
        url = data.get("next_page")
    return json.dumps(audits, indent=2)


_BULK_FETCH_MAX_RETRIES = 3
_BULK_FETCH_MAX_BACKOFF_SECONDS = 30


def _fetch_comments_with_retry(ticket_id: int) -> list[dict]:
    """Fetch all comments for one ticket, paginated, with 429 backoff.

    Honors Zendesk's Retry-After header up to a cap; capped retries so a
    sustained-429 storm fails fast instead of stalling all workers.
    """
    comments: list[dict] = []
    url: str | None = f"/tickets/{ticket_id}/comments.json"
    while url:
        for attempt in range(_BULK_FETCH_MAX_RETRIES):
            resp = _client.get(url)
            if resp.status_code == 429 and attempt < _BULK_FETCH_MAX_RETRIES - 1:
                retry_after = resp.headers.get("retry-after", "5")
                try:
                    wait = int(retry_after)
                except ValueError:
                    wait = 5
                time.sleep(min(max(wait, 1), _BULK_FETCH_MAX_BACKOFF_SECONDS))
                continue
            resp.raise_for_status()
            data = resp.json()
            break
        comments.extend(_project_comment(c) for c in data.get("comments", []))
        url = data.get("next_page")
    return comments


@mcp.tool()
def get_comments_for_tickets(ticket_ids: list[int], max_concurrency: int = 8) -> str:
    """Bulk-fetch comments for many tickets in parallel.

    Speeds up per-ticket analysis (e.g. queue-level triage) across an entire
    view: fetches up to `max_concurrency` ticket comment threads concurrently
    and returns a dict keyed by ticket_id, with the same per-comment shape as
    `get_ticket_comments` (links, images, attachments included). Follows
    pagination per ticket and retries 429s with Retry-After backoff.

    Args:
        ticket_ids: List of ticket IDs to fetch (max 200).
        max_concurrency: Maximum simultaneous Zendesk requests (1–20, default 8).
    """
    if len(ticket_ids) > 200:
        return json.dumps({"error": "Maximum 200 tickets per bulk fetch."})

    workers = max(1, min(max_concurrency, 20))

    results: dict[str, list] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_comments_with_retry, tid): tid for tid in ticket_ids}
        for fut in as_completed(futures):
            tid = futures[fut]
            try:
                results[str(tid)] = fut.result()
            except Exception as exc:  # noqa: BLE001 — one failed ticket must not kill the batch
                errors[str(tid)] = str(exc)

    out: dict = {"comments_by_ticket": results}
    if errors:
        out["errors"] = errors
    return json.dumps(out, indent=2)


_IMAGE_FORMAT_ALLOWLIST = {"png", "jpeg", "gif", "webp"}


@mcp.tool()
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

    with httpx.stream(
        "GET",
        url,
        auth=(f"{EMAIL}/token", API_KEY),
        timeout=60.0,
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


# ---------------------------------------------------------------------------
# Ticket Management Tools (management + admin)
# ---------------------------------------------------------------------------

if ACCESS_LEVEL in ("management", "admin"):

    @mcp.tool()
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
    def add_ticket_tags(ticket_id: int, tags: list[str]) -> str:
        """Add tags to a ticket without removing existing ones.

        Args:
            ticket_id: The ticket to tag.
            tags: Tags to add.
        """
        existing = _get(f"/tickets/{ticket_id}.json")
        current_tags = existing["ticket"].get("tags", [])
        merged = list(set(current_tags + tags))
        data = _put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": merged}})
        return json.dumps({"id": ticket_id, "tags": data["ticket"].get("tags", [])}, indent=2)

    @mcp.tool()
    def remove_ticket_tags(ticket_id: int, tags: list[str]) -> str:
        """Remove specific tags from a ticket.

        Args:
            ticket_id: The ticket to update.
            tags: Tags to remove.
        """
        existing = _get(f"/tickets/{ticket_id}.json")
        current_tags = existing["ticket"].get("tags", [])
        updated = [t for t in current_tags if t not in tags]
        data = _put(f"/tickets/{ticket_id}.json", {"ticket": {"tags": updated}})
        return json.dumps({"id": ticket_id, "tags": data["ticket"].get("tags", [])}, indent=2)


# ---------------------------------------------------------------------------
# Full Admin Tools (admin only)
# ---------------------------------------------------------------------------

if ACCESS_LEVEL == "admin":

    @mcp.tool()
    def merge_tickets(target_ticket_id: int, source_ticket_ids: list[int]) -> str:
        """Merge one or more tickets into a target ticket.

        The source tickets will be closed and their comments merged into the target.

        Args:
            target_ticket_id: The ticket that will remain open and receive merged content.
            source_ticket_ids: Ticket IDs to merge into the target.
        """
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
    def delete_ticket(ticket_id: int) -> str:
        """Permanently delete a Zendesk ticket. This cannot be undone.

        Args:
            ticket_id: The ticket to delete.
        """
        return _delete(f"/tickets/{ticket_id}.json")

    @mcp.tool()
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


if __name__ == "__main__":
    mcp.run(transport="stdio")
