# Feathr Zendesk MCP Server

A lightweight, homegrown MCP server that gives Claude Code access to our Zendesk instance.

## Access Levels

| Level | Tools | Who should use it |
|-------|-------|-------------------|
| **readonly** | Search/read tickets, users, views, orgs (7 tools) | Everyone |
| **management** | readonly + create/update tickets, comments, tags (12 tools) | Team leads, support agents |
| **admin** | management + user/org CRUD, merge, bulk ops, delete (21 tools) | Zendesk admins only |

## Available Tools

### Read-Only (all levels)
- `search_tickets` — Search tickets using Zendesk query syntax
- `get_ticket` — Get full ticket details by ID
- `get_ticket_comments` — Get all comments/replies on a ticket
- `get_user` — Look up a user by ID
- `search_users` — Search users by name or email
- `get_view_tickets` — Execute a saved view and return its tickets
- `list_views` — List all available views with IDs

### Ticket Management (management + admin)
- `create_ticket` — Create a new ticket
- `update_ticket` — Update ticket status, priority, assignee, tags, etc.
- `add_ticket_comment` — Add a public reply or internal note
- `add_ticket_tags` — Add tags without removing existing ones
- `remove_ticket_tags` — Remove specific tags

### Full Admin (admin only)
- `merge_tickets` — Merge source tickets into a target
- `create_user` / `update_user` — Manage users
- `get_organization` / `search_organizations` — Look up orgs
- `create_organization` / `update_organization` — Manage orgs
- `delete_ticket` — Permanently delete a ticket
- `bulk_update_tickets` — Update up to 100 tickets at once

---

## Installation via Claude Code

Paste this repo link into Claude Code and ask it to install the Zendesk MCP server. Claude will walk you through it interactively.

If you'd rather do it manually, follow the steps below.

---

<!-- CLAUDE:INSTALL-START
The section below contains instructions for Claude Code to follow when a user asks
to "install" or "set up" this Zendesk MCP server. Follow these steps interactively,
asking the user questions and waiting for their responses at each decision point.
-->

## Claude Code Installation Instructions

When a user asks you to install this Zendesk MCP server, follow these steps in order. This is an interactive process — ask questions and wait for answers before proceeding.

### Step 1: Ask about access level

Ask the user:

> **Which access level do you need?**
>
> 1. **Read-only** — Search and read tickets, users, and views. Good for investigation and triage.
> 2. **Ticket management** — Everything in read-only, plus create/update tickets, add comments, and manage tags. Good for support agents and team leads.
> 3. **Full admin** — Everything above, plus manage users and orgs, merge tickets, bulk operations, and delete tickets. **Only select this if you are an actual Zendesk admin — honor system.**
>
> Pick 1, 2, or 3.

Map their answer:
- 1 → `ACCESS_LEVEL=readonly`
- 2 → `ACCESS_LEVEL=management`
- 3 → `ACCESS_LEVEL=admin`

### Step 2: Clone the repo and set up the venv

Run these commands:

```bash
mkdir -p ~/.claude/mcp-servers
git clone git@github.com:Feathr/zendesk-mcp.git ~/.claude/mcp-servers/zendesk
cd ~/.claude/mcp-servers/zendesk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### Step 3: Create the .env file

Copy the example and set the access level from Step 1:

```bash
cp ~/.claude/mcp-servers/zendesk/.env.example ~/.claude/mcp-servers/zendesk/.env
```

Then edit `~/.claude/mcp-servers/zendesk/.env` and set `ACCESS_LEVEL` to the value from Step 1.

**For the API key:** Tell the user:

> **To get your Zendesk API key, reach out to Coog (Cris) on GChat.** He'll set you up with a token. Once you have it, update your `.env` file with:
> - `ZENDESK_EMAIL` — your `@feathr.co` email
> - `ZENDESK_API_KEY` — the token Coog gives you
>
> Let me know when your `.env` file is ready and I'll continue the setup.

**Wait for the user to confirm before proceeding.**

### Step 4: Verify the server loads

Run:

```bash
cd ~/.claude/mcp-servers/zendesk && .venv/bin/python -c "import server; print('Server loaded OK')"
```

If this fails with a `KeyError`, the `.env` file is missing values. If it fails with `ValueError`, the `ACCESS_LEVEL` is invalid.

### Step 5: Register with Claude Code

Run:

```bash
claude mcp add zendesk -s user -- ~/.claude/mcp-servers/zendesk/.venv/bin/python ~/.claude/mcp-servers/zendesk/server.py
```

The `-s user` flag makes it available globally across all projects.

### Step 6: Confirm success

Tell the user:

> **Setup complete!** Restart Claude Code and the Zendesk tools will be available in all your projects. Try asking me to "search for open Zendesk tickets" to test it out.

<!-- CLAUDE:INSTALL-END -->

---

## Manual Setup

### Prerequisites
- Python 3.10+
- Claude Code installed

### Steps

```bash
# Clone
mkdir -p ~/.claude/mcp-servers
git clone git@github.com:Feathr/zendesk-mcp.git ~/.claude/mcp-servers/zendesk

# Set up venv
cd ~/.claude/mcp-servers/zendesk
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env with your credentials and access level
# Contact Coog for your API key

# Verify
.venv/bin/python -c "import server; print('Server loaded OK')"

# Register with Claude Code
claude mcp add zendesk -s user -- ~/.claude/mcp-servers/zendesk/.venv/bin/python ~/.claude/mcp-servers/zendesk/server.py
```

## Troubleshooting

**Tools don't appear in Claude Code**
- Restart Claude Code after registering the MCP server
- Run `claude mcp list` to verify registration

**Auth errors (401/403)**
- Verify `.env` values — email must match the Zendesk account
- Check the API token for extra whitespace
- Confirm "Token Access" is enabled in Zendesk Admin > Apps and integrations > Zendesk API

**Module not found errors**
- Make sure the `claude mcp add` command points to the venv Python (`.venv/bin/python`)
- Re-run `.venv/bin/pip install -r requirements.txt`

**Invalid ACCESS_LEVEL error**
- Must be exactly `readonly`, `management`, or `admin` (lowercase)

## Updating

To pull the latest version:

```bash
cd ~/.claude/mcp-servers/zendesk
git pull
.venv/bin/pip install -r requirements.txt
```

Then restart Claude Code.

## Security

- Your API token lives in `~/.claude/mcp-servers/zendesk/.env` on your local machine only (gitignored)
- Each person uses their own API token, so actions are attributable in Zendesk audit logs
- Access levels are enforced at the server level — tools that aren't in your level don't exist in the MCP registration
- The entire server is ~500 lines of Python — easy to read and audit
