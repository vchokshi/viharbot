"""Static checks of the Kepler steward image definition. No Docker needed.

The properties that matter for a minimal, unprivileged agent image are pinned here so a later edit
cannot quietly widen it. The image is exercised for real by deploy/agentcore/kepler/smoke-test.sh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "Dockerfile.kepler"
IGNORE = ROOT / "Dockerfile.kepler.dockerignore"
REQUIREMENTS = ROOT / "requirements-kepler.txt"
pytestmark = pytest.mark.skipif(not DOCKERFILE.exists(), reason="Dockerfile.kepler is not here")


def lines(path: Path) -> list[str]:
    return [ln.strip() for ln in path.read_text().splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_base_image_is_pinned_to_a_minor_version_and_never_latest():
    froms = [ln for ln in lines(DOCKERFILE) if ln.upper().startswith("FROM ")]
    assert froms and all(re.search(r"python:3\.\d+-slim-bookworm", ln) for ln in froms)
    assert not any(":latest" in ln or "@" not in ln and ":" not in ln for ln in froms)


def test_the_build_refuses_any_target_but_arm64():
    text = DOCKERFILE.read_text()
    assert text.count('"${TARGETARCH}" = "arm64"') == 2  # in both stages
    assert "--platform=" not in text  # no constant platform; the guard is explicit instead


def test_it_runs_as_an_unprivileged_user_on_port_8080():
    body = "\n".join(lines(DOCKERFILE))
    assert re.search(r"^USER 1001:1001$", body, re.M)
    assert "EXPOSE 8080" in body and "PORT=8080" in body
    assert body.index("USER 1001:1001") > body.index("COPY src/viharbot/steward")


def test_it_copies_only_what_it_needs_and_never_the_whole_repository():
    copies = [ln for ln in lines(DOCKERFILE) if ln.startswith("COPY ")]
    for ln in copies:
        assert " . " not in ln and not re.search(r"COPY (--\S+ )*\. ", ln), ln
    joined = "\n".join(copies)
    for needed in ("requirements-kepler.txt", "src/viharbot/steward", "src/viharbot/body_limit.py",
                   "src/viharbot/__init__.py", ".github/scripts/cla_gate.py",
                   ".github/scripts/contributor_registry.py"):
        assert needed in joined, needed
    assert ".env" not in joined and "certs" not in joined and "tests" not in joined


def test_the_dockerignore_is_an_allowlist_that_excludes_everything_else():
    entries = lines(IGNORE)
    assert entries[0] == "*"
    allowed = {e[1:] for e in entries if e.startswith("!")}
    assert allowed == {"requirements-kepler.txt", "src/viharbot/__init__.py",
                       "src/viharbot/body_limit.py", "src/viharbot/steward",
                       ".github/scripts/cla_gate.py", ".github/scripts/contributor_registry.py"}
    assert not any(".env" in e or "aws" in e.lower() for e in allowed)


def test_the_image_holds_no_secret_and_sets_no_credentials():
    text = DOCKERFILE.read_text().lower()
    for risky in ("aws_access_key", "aws_secret", "aws_session_token", "github_token", "private_key",
                  "litellm_api_key", "password", "token="):
        assert risky not in text, risky
    env_lines = [ln for ln in lines(DOCKERFILE) if ln.upper().startswith("ENV") or ln.startswith(("PYTHONPATH", "KEPLER", "PYTHON", "PORT"))]
    assert any("KEPLER_CLA_GATE_DIR=/app/gate" in ln for ln in env_lines)


def test_only_the_four_direct_dependencies_are_installed_and_none_is_a_model_sdk():
    packages = [re.split(r"[=<>~!\[]", ln)[0].lower() for ln in lines(REQUIREMENTS)]
    assert sorted(packages) == ["boto3", "cryptography", "fastapi", "uvicorn"]
    for ln in lines(REQUIREMENTS):
        assert "==" in ln, ln  # pinned
    for banned in ("litellm", "openai", "anthropic", "strands", "bedrock", "langchain"):
        assert not any(banned in p for p in packages)


def test_the_start_command_uses_the_factory_so_a_bad_configuration_cannot_serve():
    cmd = "\n".join(ln for ln in lines(DOCKERFILE) if ln.startswith("CMD"))
    assert "--factory viharbot.steward.runtime:create_app_from_env" in cmd and "0.0.0.0" in cmd
    assert "--reload" not in cmd and "--workers" not in cmd  # one process: the poll lock is per process


def test_the_image_has_a_healthcheck_against_ping():
    body = "\n".join(lines(DOCKERFILE))
    assert "HEALTHCHECK" in body and "/ping" in body
