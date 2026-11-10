"""Offline test setup: fake Zendesk settings, set before anything imports `server`.

Values are assigned directly (not `setdefault`) so they win over a real `.env`:
`server.py` calls `load_dotenv` with `override=False`, which keeps variables that are already set.
"""

import os

os.environ["ZENDESK_SUBDOMAIN"] = "example"
os.environ["ZENDESK_EMAIL"] = "env@example.com"
os.environ["ZENDESK_API_KEY"] = "env-key"
# Set so the suite keeps importing if this var becomes required.
os.environ["ZENDESK_HELP_CENTER_HOST"] = "help.example.com"
os.environ["ACCESS_LEVEL"] = "readonly"
