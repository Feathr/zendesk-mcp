# Claude Code Configuration - zendesk-mcp

Guidelines for agents working in this repo — writing code, opening PRs, and reviewing PRs.
Adapted from anhinga's coding guidelines where relevant to a small MCP server.

## Critical Constraints

**NEVER install packages globally.** No bare `pip install`, `npm install -g`, etc. Use a
virtualenv or `uv`/`uvx`.

**Keep the `mcp` dependency pinned below 2.0.0.** The pin in `pyproject.toml`
(`mcp[cli]>=1.0.0,<2.0.0`) is intentional; do not loosen it without explicit discussion.

**Never commit directly to `main`.** Branch first. Commit/push only when asked.

## Repository Overview

**zendesk-mcp** is a single-file MCP server (`server.py`) exposing Zendesk tools over stdio,
built on `FastMCP` and `httpx`.

- Tool availability is gated by the `ACCESS_LEVEL` env var: `readonly` (search/read/count/
  export), `management` (+ create/update tickets, comments, tags), `admin` (+ user/org CRUD,
  merge, bulk ops, delete). Gating is done with `if ACCESS_LEVEL ...` blocks around the
  `@mcp.tool()` registrations — a tool that isn't registered can't be called.
- Configuration comes from `.env` next to `server.py`: `ZENDESK_SUBDOMAIN`, `ZENDESK_EMAIL`,
  `ZENDESK_API_KEY`, `ACCESS_LEVEL`. Missing required vars fail at import — keep it that way.

## Detected Tooling

| Task     | Command                  |
|----------|--------------------------|
| Format   | `uvx ruff format .`      |
| Lint     | `uvx ruff check .`       |
| Lint Fix | `uvx ruff check --fix .` |

Ruff is configured in `pyproject.toml`: line length 100 (119 for comments/docstrings),
target py310, rules B (bugbear), I (isort), E (pycodestyle), max complexity 10.
Run format + lint before proposing any PR. There is no test suite; review rigor and
manual verification against a real Zendesk instance stand in for it.

## Code Style

Follow the [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
with these priorities:

- **Prioritize readability over conciseness.** Far more time is spent reading code than
  writing it. Prefer an explicit loop over a dense comprehension; a comment is not a
  substitute for readable code.
- **Use descriptive variable and function names.** Don't shorten names to save line length.
- **No `assert` statements in production code** — they vanish under `python -O`. Raise
  explicit exceptions instead.
- **Prefer early returns** over deeply nested conditionals.
- **Prefer `X | None` over `Optional[X]`** (PEP 604; repo targets py310+).
- **Group logical components into functions.** Aim for under ~50 lines per function; not a
  hard PR gate, but a signal the function is doing too much.
- **Functions should have meaningful return values.** Avoid side-effect-only functions.
- **Google-style docstrings** on all public functions and tools, with Args/Returns/Raises
  where applicable.

## Type Annotations

- Type all parameters, return values, and non-obvious variables.
- Tool parameter annotations are load-bearing: FastMCP derives the tool's input schema from
  them. An untyped or wrongly typed parameter ships a broken schema to every client.

## MCP Tool Conventions

These are the repo-specific rules that matter most in review:

- **Tool docstrings are the product.** The docstring on an `@mcp.tool()` function is the
  tool description an LLM client reads to decide when and how to call it. It must state
  what the tool does, document every arg (Google-style `Args:` section), and mention
  non-obvious behavior (truncation, sorting constraints, side effects). Review docstring
  changes as carefully as code changes.
- **Tools return compact JSON strings** (`json.dumps(..., indent=2)`), never raw API
  payloads. Project responses through the `_project_*` helpers (`_project_ticket`,
  `_project_comment`) or an equivalent explicit dict so responses stay small and stable.
  Zendesk payloads are huge; dumping them wholesale bloats the caller's context.
- **Use the shared HTTP helpers** (`_get`, `_post`, `_put`, `_delete`) — never construct a
  second `httpx.Client`. For loops issuing many requests, use `_get_with_retry`, which
  honors `Retry-After` on 429 with capped attempts/backoff so a rate-limit storm fails fast
  instead of stalling.
- **Register new tools in the right access-level block.** Anything that mutates Zendesk
  belongs under `management` or `admin`, never in the readonly section. Destructive or
  irreversible operations (delete, merge, bulk update) are admin-only, and their docstrings
  must say what is irreversible.
- **Don't weaken the security guards.** `fetch_attachment` refuses non-HTTPS URLs and hosts
  other than the configured subdomain (SSRF guard on an auth-bearing client), streams with
  an early size cap, and does not follow redirects. Bounded loops (`export_search_results`)
  cap total results and attempts. Any change touching these needs explicit justification in
  the PR body.
- **Ticket/user content is untrusted input.** This server feeds customer-authored text to
  LLMs; keep the prompt-injection note in the README accurate and don't add tools that
  execute or eval fetched content.
- **Keep the docs in sync.** A PR that adds/removes/renames a tool or changes an access
  level must update: the module docstring in `server.py` (including its per-level tool
  counts) and the tool tables in `README.md`.

## Pull Request Guidelines

- **PRs should stay under ~400 lines.** If a change would exceed that, stop and propose a
  breakdown first. Separate refactors from features — never mix cleanup with new
  functionality in one PR.
- **All changes require review from @crisecheguren** (enforced by `.github/CODEOWNERS`).
- Keep PR and issue text concise; verbosity causes confusion in review.
- No issue/PR numbers in code comments — they go stale and add noise.
- No exact "N tools tested" style counts in PR bodies; describe verification qualitatively.
- Intentional lint suppressions use targeted `# noqa: <rule>` with a reason, never blanket
  ignores.
