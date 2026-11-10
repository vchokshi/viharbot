"""The memory service's Compose, image and dependency definitions (ADR-009, issue #40, PR 1).

Static checks on repository files: no container is started and nothing touches the network.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = (ROOT / "compose.yaml").read_text()


def block(name: str) -> str:
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|^\S|\Z)", COMPOSE, re.S | re.M)
    assert match, name
    return match.group(1)


def test_postgres_is_private_pinned_persistent_and_healthchecked():
    postgres = block("postgres")
    assert re.search(r"^    image: postgres:\d+\.\d+-alpine\d+\.\d+$", postgres, re.M), "pin an exact tag"
    assert "latest" not in postgres
    assert "ports:" not in postgres and "expose:" in postgres  # no published port
    assert re.search(r"^      - memory_pg_data:/var/lib/postgresql/data$", postgres, re.M)
    assert re.search(r"^  memory_pg_data:$", COMPOSE, re.M)  # a named volume, declared
    assert "pg_isready" in postgres and "healthcheck:" in postgres
    assert re.search(r"networks:\n      - agent-network", postgres)
    assert 'profiles: ["memory"]' in postgres


def test_database_credentials_come_from_the_environment_with_no_defaults_in_source():
    for service in ("postgres", "memory-migrate", "memory"):
        for line in block(service).splitlines():
            if re.search(r"PASSWORD|BOOTSTRAP_USER", line) and "${" in line:
                assert re.search(r"\$\{[A-Z_]+:-\}", line), line  # an empty fallback, never a value
    assert not re.search(r"PASSWORD: [^$\s\"]", COMPOSE), "a literal password in compose.yaml"


def test_only_the_memory_service_and_its_migration_job_hold_database_credentials():
    holders = [
        name for name in ("web", "research", "security", "fact-check", "source-quality",
                          "privacy-review", "synthesis", "pixel", "sage", "orchestrator", "tests")
        if re.search(r"MEMORY_(MIGRATE_)?DB_|POSTGRES_|MEMORY_PG_", block(name))
    ]
    assert holders == [], f"an agent or the web tier references the memory database: {holders}"
    assert "MEMORY_DB_PASSWORD" in block("memory") and "MEMORY_MIGRATE_DB_PASSWORD" not in block("memory")
    assert "MEMORY_MIGRATE_DB_PASSWORD" in block("memory-migrate") and "MEMORY_DB_PASSWORD" not in block("memory-migrate")
    assert "POSTGRES_" not in block("memory") and "BOOTSTRAP" not in block("memory")  # no superuser


def test_the_memory_service_waits_for_postgres_health_and_the_migration():
    memory = block("memory")
    assert re.search(r"postgres:\n        condition: service_healthy", memory)
    assert re.search(r"memory-migrate:\n        condition: service_completed_successfully", memory)
    assert "ports:" not in memory and "expose:" in memory and "healthcheck:" in memory
    assert "VIHARBOT_KEY_REGISTRY: /run/memory/key-registry.toml" in memory
    assert "VIHARBOT_SERVICE_AUTH" not in memory  # there is no off switch to configure
    assert 'restart: "no"' in block("memory-migrate")


def test_the_memory_stack_is_opt_in_so_the_default_stack_and_ci_are_unchanged():
    for service in ("postgres", "memory-migrate", "memory"):
        assert 'profiles: ["memory"]' in block(service)
    web = block("web")
    assert "memory" not in web.lower().replace("memory_dir", "") and "postgres" not in web.lower()


def test_the_digitalocean_spec_is_untouched():
    spec = (ROOT / "deploy" / "digitalocean" / "app.yaml").read_text().lower()
    assert "postgres" not in spec and "memory_db" not in spec and "memory-service" not in spec


def test_the_init_script_creates_two_roles_from_environment_passwords_only():
    script = (ROOT / "deploy" / "postgres" / "init" / "10-memory-roles.sh").read_text()
    assert script.startswith("#!/bin/sh") and "set -eu" in script
    assert script.count("CREATE ROLE") == 2 and "memory_migrator" in script and "memory_service" in script
    assert "SUPERUSER" not in script and "CREATEDB" not in script and "CREATEROLE" not in script
    assert ":'migrator_pw'" in script and ":'service_pw'" in script
    assert "REVOKE ALL ON DATABASE" in script
    assert (ROOT / "deploy" / "postgres" / "init" / "10-memory-roles.sh").stat().st_mode & 0o111


def test_the_memory_image_installs_the_driver_and_runs_unprivileged():
    text = (ROOT / "Dockerfile.memory").read_text()
    assert '".[memory]"' in text and "USER 1001:1001" in text
    assert "create_app_from_environment --factory" in text
    assert not re.search(r"^\s*(ARG|ENV)\s+\S*(KEY|TOKEN|SECRET|PASSWORD)", text, re.M | re.I)
    for other in ("Dockerfile.specialist", "Dockerfile.research", "Dockerfile.security", "Dockerfile.web",
                  "Dockerfile.pixel", "Dockerfile.sage", "Dockerfile.orchestrator"):
        assert "[memory]" not in (ROOT / other).read_text(), f"{other} must not install the driver"


def test_the_driver_is_one_pinned_dependency_in_the_memory_and_test_extras_only():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert not any("psycopg" in dep for dep in project["dependencies"])
    extras = project["optional-dependencies"]
    assert extras["memory"] == ["psycopg[binary]==3.3.8"]
    assert "psycopg[binary]==3.3.8" in extras["test"]
    assert not any("psycopg" in dep for dep in extras.get("dev", []) + extras.get("pixel", []))
