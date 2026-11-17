"""Offline test setup: fake Zendesk settings, set before anything imports `server`.

Values are assigned directly (not `setdefault`) so they win over a real `.env`:
`server.py` calls `load_dotenv` with `override=False`, which keeps variables that are already set.
`MCP_TRANSPORT` is pinned to stdio so a developer `.env` cannot load the suite in HTTP mode.

`HOME` points at a fresh temp dir, set before `stdio_oauth` computes `DEFAULT_TOKEN_DIR` at import,
so no test can read or refresh the developer's real `~/.config/zendesk-mcp/tokens.json`.
"""

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

TEST_HOME = Path(tempfile.mkdtemp(prefix="zendesk-mcp-test-home-"))
os.environ["HOME"] = str(TEST_HOME)
# Removed when the test process exits; only this temp dir, never a real HOME.
atexit.register(shutil.rmtree, TEST_HOME, ignore_errors=True)

os.environ["ZENDESK_SUBDOMAIN"] = "example"
os.environ["ZENDESK_EMAIL"] = "env@example.com"
os.environ["ZENDESK_API_KEY"] = "env-key"
# Set so the suite keeps importing if this var becomes required.
os.environ["ZENDESK_HELP_CENTER_HOST"] = "help.example.com"
os.environ["ACCESS_LEVEL"] = "readonly"
os.environ["MCP_TRANSPORT"] = "stdio"
# HTTP mode requires it; matches client_id 7 in the stubbed tokens/current answers.
os.environ["ZENDESK_OAUTH_CLIENT_ID"] = "7"
# The stdio login's OAuth client Identifier, so a `main()` test never picks up a real `.env` value.
os.environ["ZENDESK_STDIO_OAUTH_CLIENT_ID"] = "zendesk_mcp_stdio"


def remove_leftover_token_file(token_dir: Path) -> bool:
    """Delete `tokens.json` in `token_dir` when a test left one there under the test HOME.

    It acts only under `TEST_HOME`, so it can never delete a real sign-in.

    Args:
        token_dir: The token directory to check, normally `stdio_oauth.DEFAULT_TOKEN_DIR`.

    Returns:
        True if a file was removed.
    """
    if TEST_HOME.resolve() not in token_dir.resolve().parents:
        return False
    token_file = token_dir / "tokens.json"
    if not token_file.exists():
        return False
    token_file.unlink()
    return True


@pytest.fixture(autouse=True)
def no_leftover_token_file() -> Iterator[None]:
    """Fail a test that leaves a token file in the shared test HOME, after removing it.

    A leftover file would make the fallback tests that run later depend on test order.
    """
    yield
    import stdio_oauth  # imported here, after HOME is set above

    if remove_leftover_token_file(stdio_oauth.DEFAULT_TOKEN_DIR):
        pytest.fail("test left a token file in the shared test HOME")
