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
    assert "profiles:" not in postgres  # part of the default stack (founder decision, PR 2)
    # Healthy only once the FINAL server is up: a TCP check, because the temporary init-time server
    # listens on the unix socket only (a socket check passed too early and memory-migrate failed).
    assert "pg_isready -h 127.0.0.1" in postgres


def test_database_credentials_come_from_generated_files_with_no_values_or_defaults_in_source():
    expected = {
        "postgres": "./data/dev-auth/memory-db/postgres.env",
        "memory-migrate": "./data/dev-auth/memory-db/migrate.env",
        "memory": "./data/dev-auth/memory-db/service.env",
    }
    for service, path in expected.items():
        text = block(service)
        # optional at parse time so `docker compose config` works before setup; absent means empty
        # passwords, which the services refuse (checked below), so the stack fails closed
        assert re.search(rf"env_file:\n      - path: {re.escape(path)}\n        required: false", text), service
        for line in text.splitlines():
            assert not re.search(r"PASSWORD|BOOTSTRAP", line), (service, line)  # no value, no default
    assert ":?" not in COMPOSE  # no ${VAR:?} form, which would break `docker compose config`
    assert not re.search(r"PASSWORD: [^$\s\"]", COMPOSE), "a literal password in compose.yaml"
    # Each role's password is in its own file only: the service never sees the migration role's.
    script = (ROOT / "scripts" / "dev_service_auth.py").read_text()
    assert 'MEMORY_MIGRATE_DB_PASSWORD": migrator' in script and 'MEMORY_DB_PASSWORD": service' in script
    assert "credentials or certificates are missing" in (ROOT / "Dockerfile.memory").read_text()  # fails closed, loudly


def test_only_the_memory_service_and_its_migration_job_hold_database_credentials():
    holders = [
        name for name in ("web", "research", "security", "fact-check", "source-quality",
                          "privacy-review", "synthesis", "pixel", "sage", "orchestrator", "tests")
        if re.search(r"MEMORY_(MIGRATE_)?DB_|POSTGRES_|MEMORY_PG_", block(name))
    ]
    assert holders == [], f"an agent or the web tier references the memory database: {holders}"
    assert "service.env" in block("memory") and "migrate.env" not in block("memory")
    assert "migrate.env" in block("memory-migrate") and "service.env" not in block("memory-migrate")
    assert "postgres.env" not in block("memory") + block("memory-migrate")  # no superuser or other role's password
    assert "POSTGRES_" not in block("memory") and "BOOTSTRAP" not in block("memory")


def test_the_memory_service_waits_for_postgres_health_and_the_migration():
    memory = block("memory")
    assert re.search(r"postgres:\n        condition: service_healthy", memory)
    assert re.search(r"memory-migrate:\n        condition: service_completed_successfully", memory)
    assert "ports:" not in memory and "expose:" in memory and "healthcheck:" in memory
    assert "VIHARBOT_KEY_REGISTRY: /run/memory/key-registry.toml" in memory
    assert "VIHARBOT_SERVICE_AUTH" not in memory  # there is no off switch to configure
    assert 'restart: "no"' in block("memory-migrate")


def test_the_memory_stack_is_in_the_default_stack_and_nothing_depends_on_it():
    for service in ("postgres", "memory-migrate", "memory"):
        assert "profiles:" not in block(service), service  # a plain `docker compose up` runs it
    for name in ("web", "research", "security", "fact-check", "source-quality", "privacy-review",
                 "synthesis", "pixel", "sage", "orchestrator"):
        # Memory is best effort: no service waits for the memory stack to start (depends_on entries
        # are indented six spaces).
        assert not re.findall(r"^      (?:memory|postgres|memory-migrate):", block(name), re.M), name
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
