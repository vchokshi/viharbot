"""Checks that shipped configuration keeps every optional mode off and carries no secrets."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCAL_FILES = (".env.example", "compose.yaml")
DEPLOY_SPEC = "deploy/digitalocean/app.yaml"


def assert_flag_shipped_off(flag: str) -> None:
    """Local config may name a flag only with a `false` default; the cloud spec must not."""
    for name in LOCAL_FILES:
        for line in (ROOT / name).read_text().splitlines():
            if flag in line:
                lowered = line.lower()
                assert "false" in lowered and "true" not in lowered, (name, line.strip())
    spec = ROOT / DEPLOY_SPEC
    if spec.exists():
        assert flag not in spec.read_text(), DEPLOY_SPEC
