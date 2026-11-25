"""Environment-specific URLs for the Agent Memory Explorer and Command Central.

Nothing here depends on public DNS: with no configuration the explorer link is the relative
path on this same web app and Command Central is reported as planned, not linked.
"""

from __future__ import annotations

import os
import re
from urllib.parse import quote

_URL = re.compile(r"^(https://[A-Za-z0-9.-]+(:\d+)?|http://(localhost|127\.0\.0\.1)(:\d+)?)(/[A-Za-z0-9._~%/-]*)?$")
ENVIRONMENTS = ("local", "stage", "production")


def _checked(name: str) -> str:
    """A configured base URL, or "". https or loopback http only, no credentials, query or fragment."""
    value = os.getenv(name, "").strip().rstrip("/")
    if not value:
        return ""
    if not _URL.fullmatch(value):
        raise RuntimeError(f"{name} must be an https URL (or http on localhost) with no credentials, query or fragment")
    return value


def explorer_path(agent_id: str) -> str:
    return f"/agents/{quote(agent_id, safe='')}"


def explorer_url(agent_id: str) -> str:
    """Where an agent's explorer page lives. Relative to this app unless a base URL is set."""
    return _checked("MEMORY_EXPLORER_BASE_URL") + explorer_path(agent_id)


def command_central_url() -> str:
    """Founder Mission Control, or "" while it is not deployed (issues #301 and #305)."""
    return _checked("COMMAND_CENTRAL_URL")


def environment_name() -> str:
    value = os.getenv("MEMORY_EXPLORER_ENVIRONMENT", "local").strip().lower()
    return value if value in ENVIRONMENTS else "local"
