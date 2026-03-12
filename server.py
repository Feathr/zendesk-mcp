"""Zendesk MCP Server — ticket triage, management, and admin.

Access levels controlled by ACCESS_LEVEL env var:
    readonly   — search/read tickets, users, views, orgs (7 tools)
    management — readonly + create/update tickets, comments, tags (12 tools)
    admin      — management + user/org CRUD, merge, bulk ops, delete (21 tools)
"""

import json
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from the same directory as this script
load_dotenv(Path(__file__).parent / ".env")

SUBDOMAIN = os.environ["ZENDESK_SUBDOMAIN"]
EMAIL = os.environ["ZENDESK_EMAIL"]
API_KEY = os.environ["ZENDESK_API_KEY"]
ACCESS_LEVEL = os.environ.get("ACCESS_LEVEL", "readonly").lower()

if ACCESS_LEVEL not in ("readonly", "management", "admin"):
    raise ValueError(f"Invalid ACCESS_LEVEL: {ACCESS_LEVEL!r}. Must be readonly, management, or admin.")

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
# Read-Only Tools (all access levels)
# ---------------------------------------------------------------------------


@mcp.tool()
def search_tickets(query: str) -> str:
    """Search Zendesk tickets using Zendesk search syntax.

    Examples:
        search_tickets("status:open assignee:me")
        search_tickets("subject:refund created>2026-01-01")
        search_tickets("priority:urgent status:open")

    See https://support.zendesk.com/hc/en-us/articles/203663226 for query syntax.
    """
    data = _get("/search.json", params={"query": f"type:ticket {query}"})
    results = data.get("results", [])
    if not results:
        return "No tickets found."
    tickets = []
    for t in results:
        tickets.append({
            "id": t["id"],
            "subject": t.get("subject", ""),
            "status": t.get("status", ""),
            "priority": t.get("priority", ""),
            "created_at": t.get("created_at", ""),
            "updated_at": t.get("updated_at", ""),
            "assignee_id": t.get("assignee_id"),
            "requester_id": t.get("requester_id"),
            "tags": t.get("tags", []),
        })
    return json.dumps(tickets, indent=2)


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
    """Get all comments (replies) on a Zendesk ticket, in chronological order."""
    data = _get(f"/tickets/{ticket_id}/comments.json")
    comments = []
    for c in data.get("comments", []):
        comments.append({
            "id": c["id"],
            "author_id": c.get("author_id"),
            "body": c.get("body", ""),
            "plain_body": c.get("plain_body", ""),
            "public": c.get("public", True),
            "created_at": c.get("created_at", ""),
        })
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
        users.append({
            "id": u["id"],
            "name": u.get("name", ""),
            "email": u.get("email", ""),
            "role": u.get("role", ""),
            "organization_id": u.get("organization_id"),
        })
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
        tickets.append({
            "id": t["id"],
            "subject": t.get("subject", ""),
            "status": t.get("status", ""),
            "priority": t.get("priority", ""),
            "requester_id": t.get("requester_id"),
            "assignee_id": t.get("assignee_id"),
            "updated_at": t.get("updated_at", ""),
        })
    return json.dumps(tickets, indent=2)


@mcp.tool()
def list_views() -> str:
    """List all available Zendesk views with their IDs and names."""
    data = _get("/views.json")
    views = []
    for v in data.get("views", []):
        views.append({
            "id": v["id"],
            "title": v.get("title", ""),
            "active": v.get("active", True),
        })
    return json.dumps(views, indent=2)


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
        return json.dumps({
            "id": t["id"],
            "status": t["status"],
            "priority": t.get("priority"),
            "assignee_id": t.get("assignee_id"),
            "tags": t.get("tags", []),
        }, indent=2)

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
        data = _put(f"/tickets/{ticket_id}.json", {
            "ticket": {
                "comment": {"body": body, "public": public},
            }
        })
        return json.dumps({
            "ticket_id": data["ticket"]["id"],
            "status": data["ticket"]["status"],
            "comment_added": True,
            "public": public,
        }, indent=2)

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
        data = _post(f"/tickets/{target_ticket_id}/merge.json", {
            "ids": source_ticket_ids,
        })
        return json.dumps({
            "target_ticket_id": target_ticket_id,
            "merged_ticket_ids": source_ticket_ids,
            "status": data.get("job_status", {}).get("status", "queued"),
        }, indent=2)

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
        return json.dumps({
            "id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
        }, indent=2)

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
        return json.dumps({
            "id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
            "tags": u.get("tags", []),
        }, indent=2)

    @mcp.tool()
    def get_organization(organization_id: int) -> str:
        """Get details for a Zendesk organization by ID."""
        data = _get(f"/organizations/{organization_id}.json")
        o = data["organization"]
        return json.dumps({
            "id": o["id"],
            "name": o.get("name", ""),
            "domain_names": o.get("domain_names", []),
            "tags": o.get("tags", []),
            "group_id": o.get("group_id"),
            "created_at": o.get("created_at", ""),
        }, indent=2)

    @mcp.tool()
    def search_organizations(query: str) -> str:
        """Search Zendesk organizations by name.

        Examples:
            search_organizations("Acme Corp")
        """
        data = _get("/organizations/search.json", params={"name": query})
        orgs = []
        for o in data.get("organizations", []):
            orgs.append({
                "id": o["id"],
                "name": o.get("name", ""),
                "domain_names": o.get("domain_names", []),
                "tags": o.get("tags", []),
            })
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
        return json.dumps({
            "id": o["id"], "name": o["name"], "domain_names": o.get("domain_names", []),
        }, indent=2)

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
        return json.dumps({
            "id": o["id"], "name": o["name"],
            "domain_names": o.get("domain_names", []), "tags": o.get("tags", []),
        }, indent=2)

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
        return json.dumps({
            "updated_count": len(ticket_ids),
            "job_status": data.get("job_status", {}).get("status", "queued"),
        }, indent=2)


if __name__ == "__main__":
    mcp.run(transport="stdio")
