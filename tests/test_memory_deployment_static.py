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


# --- the proposed Stage app spec (docs/stage-memory-design.md item 11) -----------------------------
# This used to assert the spec was untouched. It now describes the proposal deliberately. The spec
# is a PROPOSAL, NOT APPROVED FOR DEPLOY: these tests read a file; nothing is applied anywhere.

SPEC_PATH = ROOT / "deploy" / "digitalocean" / "app.yaml"


def _scalar(text: str):
    text = text.strip()
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text in ("true", "false"):
        return text == "true"
    return int(text) if re.fullmatch(r"-?\d+", text) else text


def load_spec(text: str):
    """A tiny parser for the restricted YAML this spec uses (mappings, lists, scalars, comments).

    PyYAML is not a dependency of this repository, so the spec is read with this instead. It fails
    loudly on anything it does not understand rather than guessing.
    """
    rows = []
    for raw in text.splitlines():
        stripped = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if stripped.strip():
            rows.append((len(stripped) - len(stripped.lstrip()), stripped.strip()))
    position = 0

    def parse_block(indent: int):
        nonlocal position
        if rows[position][1].startswith("- "):
            items = []
            while position < len(rows) and rows[position][0] == indent and rows[position][1].startswith("- "):
                body = rows[position][1][2:]
                if re.match(r"^[A-Za-z_][\w-]*:( |$)", body):  # a mapping item: re-read the body as its first line
                    rows[position] = (indent + 2, body)
                    items.append(parse_block(indent + 2))
                else:
                    items.append(_scalar(body))
                    position += 1
            return items
        mapping = {}
        while position < len(rows) and rows[position][0] == indent and not rows[position][1].startswith("- "):
            key, _, rest = rows[position][1].partition(":")
            position += 1
            if rest.strip():
                mapping[key] = _scalar(rest)
            elif position < len(rows) and (rows[position][0] > indent or (
                    rows[position][0] == indent and rows[position][1].startswith("- "))):
                mapping[key] = parse_block(rows[position][0])
            else:
                mapping[key] = ""
        assert position == len(rows) or rows[position][0] < indent, f"cannot parse: {rows[position]}"
        return mapping

    result = parse_block(0)
    assert position == len(rows)
    return result
SPEC = load_spec(SPEC_PATH.read_text())
SERVICES = {c["name"]: c for c in SPEC["services"]}
JOBS = {c["name"]: c for c in SPEC.get("jobs", [])}
EXISTING = ("web", "research", "security", "fact-check", "source-quality", "privacy-review", "synthesis")
# Variable-name prefixes that belong to the memory feature only.
MEMORY_PREFIXES = ("VIHARBOT_MEMORY", "MEMORY_DB", "MEMORY_ADMIN", "MEMORY_MIGRATE", "MEMORY_STAGE", "MEMORY_TLS",
                   "VIHARBOT_KEY_REGISTRY", "VIHARBOT_SIGNING", "VIHARBOT_SERVICE_SUBJECT", "VIHARBOT_OUTBOUND")
EXPECTED_EXISTING_KEYS = {
    "web": {"WEB_AUTH_ENABLED", "WEB_IMAGES_BASE_URL", "WEB_USERNAME", "WEB_PASSWORD", "WEB_STATIC_DIR",
            "VIHARBOT_PPT_GENERATION_ENABLED", "RESEARCH_URL", "SECURITY_URL", "FACT_CHECK_URL",
            "SOURCE_QUALITY_URL", "PRIVACY_REVIEW_URL", "SYNTHESIS_URL"},
    "research": {"RESEARCH_MODEL", "MEMORY_DIR", "LITELLM_API_KEY"},
    "security": {"SECURITY_MODEL", "MEMORY_DIR", "LITELLM_API_KEY"},
    "fact-check": {"AGENT_KIND", "FACT_CHECK_MODEL", "MEMORY_DIR", "LITELLM_API_KEY"},
    "source-quality": {"AGENT_KIND", "SOURCE_QUALITY_MODEL", "MEMORY_DIR", "LITELLM_API_KEY"},
    "privacy-review": {"AGENT_KIND", "PRIVACY_MODEL", "MEMORY_DIR", "LITELLM_API_KEY"},
}
CHARLIE_MEMORY_KEYS = {"VIHARBOT_MEMORY_URL", "VIHARBOT_MEMORY_CA_PEM", "VIHARBOT_OUTBOUND_AUTH",
                       "VIHARBOT_SIGNING_KID", "VIHARBOT_SERVICE_SUBJECT", "VIHARBOT_SIGNING_KEY"}
BINDING_REFERENCE = re.compile(r"^\$\{memory-db\.(HOSTNAME|PORT|USERNAME|PASSWORD|DATABASE)\}$")


def env_of(component: dict) -> dict[str, dict]:
    return {e["key"]: e for e in component.get("envs", [])}


def every_component() -> list[dict]:
    return [*SERVICES.values(), *JOBS.values()]


def test_the_spec_has_the_memory_service_job_and_database_and_the_original_components():
    assert list(SERVICES) == [*EXISTING, "memory"]
    assert list(JOBS) == ["memory-bootstrap"]
    assert [d["name"] for d in SPEC["databases"]] == ["memory-db"]
    assert SPEC["databases"][0]["engine"] == "PG"


def test_web_and_the_five_other_agents_carry_no_memory_variables_and_are_otherwise_unchanged():
    for name, expected in EXPECTED_EXISTING_KEYS.items():
        keys = set(env_of(SERVICES[name]))
        assert keys == expected, name
        assert not [k for k in keys if k.startswith(MEMORY_PREFIXES) or "memory-db" in str(SERVICES[name])], name
    assert "memory-db" not in str({n: SERVICES[n] for n in EXISTING if n != "synthesis"})
    assert "memory" not in " ".join(str(v) for k, v in SERVICES["web"].items() if k != "envs").lower()


def test_only_charlie_gets_the_client_side_memory_variables():
    assert CHARLIE_MEMORY_KEYS <= set(env_of(SERVICES["synthesis"]))
    memory_keys = set(env_of(SERVICES["synthesis"])) & {k for c in every_component() for k in env_of(c)
                                                          if k.startswith(MEMORY_PREFIXES)}
    assert memory_keys == CHARLIE_MEMORY_KEYS  # no database, TLS-server or registry variable on Charlie
    assert env_of(SERVICES["synthesis"])["VIHARBOT_MEMORY_URL"]["value"] == "https://memory:8443"
    assert env_of(SERVICES["synthesis"])["VIHARBOT_SERVICE_SUBJECT"]["value"] == "charlie"
    assert env_of(SERVICES["synthesis"])["VIHARBOT_OUTBOUND_AUTH"]["value"] == "on"
    for name in ("research", "security", "fact-check", "source-quality", "privacy-review", "web"):
        assert not {"VIHARBOT_MEMORY_URL"} & set(env_of(SERVICES[name]))


def test_the_memory_service_has_no_public_route_and_checks_tcp_on_8443():
    memory = SERVICES["memory"]
    assert "http_port" not in memory and "routes" not in memory
    assert memory["internal_ports"] == [8443]
    assert memory["health_check"] == {"port": 8443}  # TCP: an HTTPS service cannot answer a plain HTTP check
    assert memory["instance_count"] == 1
    assert memory["dockerfile_path"] == "Dockerfile.memory"
    assert "ingress" not in SPEC  # the only public route is the platform default for `web`
    assert [n for n, c in SERVICES.items() if "http_port" in c] == ["web"]
    env = env_of(memory)
    assert env["PORT"]["value"] == "8443" and env["MEMORY_STAGE_ROLE"]["value"] == "serve"
    assert env["MEMORY_DB_SSLMODE"]["value"] == "verify-full" and env["MEMORY_DB_USER"]["value"] == "memory_service"
    assert "MEMORY_DB_MIGRATOR_PASSWORD" not in env and not [k for k in env if k.startswith("MEMORY_ADMIN")]


def test_the_bootstrap_job_is_a_pre_deploy_job_with_no_route():
    job = JOBS["memory-bootstrap"]
    assert job["kind"] == "PRE_DEPLOY" and job["dockerfile_path"] == "Dockerfile.memory"
    assert not {"http_port", "internal_ports", "health_check", "routes"} & set(job)
    env = env_of(job)
    assert env["MEMORY_STAGE_ROLE"]["value"] == "bootstrap"
    assert env["MEMORY_ADMIN_DB_SSLMODE"]["value"] == "verify-full"
    assert {"MEMORY_DB_MIGRATOR_PASSWORD", "MEMORY_DB_SERVICE_PASSWORD", "MEMORY_DB_CA_PEM",
            "MEMORY_ADMIN_DB_PASSWORD"} <= set(env)
    assert not {"MEMORY_TLS_KEY_PEM", "VIHARBOT_SIGNING_KEY", "MEMORY_DB_PASSWORD"} & set(env)


def test_deploy_on_push_is_false_on_every_component_that_has_a_source():
    for component in every_component():
        assert component["github"]["deploy_on_push"] is False, component["name"]
        assert component["github"]["branch"] == "main" and component["github"]["repo"] == "vchokshi/viharbot"
    assert "deploy_on_push: true" not in SPEC_PATH.read_text().lower()


def test_the_spec_holds_no_secret_value_and_every_secret_is_empty_or_a_binding_reference():
    for component in every_component():
        for key, entry in env_of(component).items():
            value = str(entry["value"])
            if entry["type"] == "SECRET":
                assert value == "" or BINDING_REFERENCE.match(value), (component["name"], key)
            if key.endswith(("PASSWORD", "_KEY", "_PEM", "KEY_REGISTRY_B64")):
                assert entry["type"] == "SECRET", (component["name"], key)  # never a plain variable
            assert "BEGIN " not in value and "postgres://" not in value and "postgresql://" not in value
            assert not re.fullmatch(r"[A-Za-z0-9+/_-]{40,}={0,2}", value), (component["name"], key)
    assert "digitaloceanspaces.com/images" in SPEC_PATH.read_text()  # the one pre-existing public URL


def test_the_database_binding_uses_placeholders_and_only_memory_components_reference_it():
    database = SPEC["databases"][0]
    assert database["cluster_name"].startswith("PLACEHOLDER_") and database["db_name"].startswith("PLACEHOLDER_")
    holders = {c["name"] for c in every_component() if "${memory-db." in str(c)}
    assert holders == {"memory", "memory-bootstrap"}
    for component in (SERVICES["memory"], JOBS["memory-bootstrap"]):
        for key, entry in env_of(component).items():
            if "${" in str(entry["value"]):
                assert BINDING_REFERENCE.match(str(entry["value"])), (component["name"], key)


def test_every_memory_variable_in_the_spec_is_one_the_code_reads():
    src = ROOT / "src" / "viharbot"
    code = {
        "memory": (src / "memory_service" / "stage_entry.py").read_text() + (src / "memory_service" / "app.py").read_text(),
        "memory-bootstrap": (src / "memory_service" / "stage_bootstrap.py").read_text() + (src / "memory_service" / "app.py").read_text(),
        "synthesis": (src / "memory_client.py").read_text() + (src / "outbound_auth.py").read_text() + (src / "service_auth.py").read_text(),
    }
    for name, text in code.items():
        component = SERVICES.get(name) or JOBS[name]
        for key in env_of(component):
            if key.startswith(MEMORY_PREFIXES):
                # Database settings are read as prefix + name (``MEMORY_ADMIN_DB_`` + ``HOST``).
                prefix = next((p for p in ("MEMORY_ADMIN_DB_", "MEMORY_DB_") if key.startswith(p)), None)
                suffix = key[len(prefix):] if prefix else key
                assert key in text or (f'"{prefix}"' in text and f'"{suffix}"' in text) or \
                    (f'"{suffix}"' in text and prefix in text), (name, key)


def test_the_stage_spec_is_marked_as_a_proposal_that_is_not_approved_for_deploy():
    text = SPEC_PATH.read_text()
    assert "PROPOSAL, NOT APPROVED FOR DEPLOY" in text
    assert (ROOT / "docs" / "stage-memory-deployment-plan.md").exists()


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
