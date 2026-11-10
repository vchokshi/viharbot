"""Shared fixtures for the memory service tests (ADR-009, issue #40, PR 1).

Offline: no model, no gateway, no network beyond an optional local PostgreSQL. Callers are
authenticated with throwaway Ed25519 keys through the real A2 verifier. The database fixtures are
selected by VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN (a superuser DSN for a throwaway server, for example
postgresql://postgres:<pw>@127.0.0.1:55432/postgres) and skip with a clear message without it.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from viharbot import service_auth as sa
from viharbot.memory_service.app import AUDIENCE, create_app
from viharbot.memory_service.policy import MemoryPolicy, PolicyProvider, load_policy
from viharbot.memory_service.repository import MemoryRepository

ADMIN_ENV = "VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN"
CONTAINER_ENV = "VIHARBOT_MEMORY_TEST_PG_CONTAINER"
T0 = datetime(2026, 11, 10, 12, 0, 0, tzinfo=timezone.utc)
AGENTS = ("nova", "scout", "cipher")
WRITE = "memory.write"
READ = "memory.read"


class Clock:
    """The service clock (datetimes). The authenticator keeps real time, tokens are minted fresh."""

    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class ExplodingRepository(MemoryRepository):
    """Proves a request was refused before storage: any use of the repository fails the test."""

    def _boom(self, *args: Any, **kwargs: Any):
        raise AssertionError("the repository must not be reached by a denied request")

    ping = get = list = create = update = classify = retire = erase = complete_erasure = _boom
    pending_erasures = _boom


@dataclass
class World:
    client: TestClient
    repo: MemoryRepository
    policies: PolicyProvider
    clock: Clock
    directory: Path
    keys: dict[str, tuple[str, Path]] = field(default_factory=dict)  # sub -> (kid, key file)
    tokens: list[str] = field(default_factory=list)
    secrets_seen: list[str] = field(default_factory=list)

    def token(
        self, sub: str, method: str, target: str, body: bytes = b"", cap: str | None = None,
        claim_sub: str | None = None,
    ) -> str:
        """A token signed with ``sub``'s key. ``claim_sub`` forges a different subject claim."""
        kid, key_file = self.keys[sub]
        token = sa.mint_assertion(
            kid=kid, sub=claim_sub or sub, audience=AUDIENCE,
            capability=cap or (READ if method == "GET" else WRITE),
            engagement="unscoped", classification="unclassified",
            method=method, target=target, body=body, key_path=str(key_file),
        )
        self.tokens.append(token)
        return token

    def call(
        self, sub: str | None, method: str, target: str, body: dict | None = None,
        *, cap: str | None = None, token: str | None = None, request_id: str | None = None,
        raw_body: bytes | None = None, claim_sub: str | None = None,
    ):
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else b"")
        headers = {"content-type": "application/json"}
        if request_id:
            headers["x-request-id"] = request_id
        if token is None and sub is not None:
            token = self.token(sub, method, target, payload, cap, claim_sub)
        if token:
            headers[sa.ASSERTION_HEADER] = token
        return self.client.request(method, target, content=payload, headers=headers)


def new_key() -> tuple[Ed25519PrivateKey, str, str]:
    key = Ed25519PrivateKey.generate()
    public = sa.b64url_encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    seed = sa.b64url_encode(
        key.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
    )
    return key, public, seed


def build_world(
    directory: Path, repo: MemoryRepository, *, policies: PolicyProvider | None = None,
    extra_subs: tuple[str, ...] = ("mallory",), clock: Clock | None = None,
    max_body_bytes: int | None = None,
) -> World:
    """An app wired to the real verifier. ``extra_subs`` hold a valid key but are in no policy."""
    clock = clock or Clock()
    entries, keys, seeds = [], {}, []
    for sub in (*AGENTS, *extra_subs):
        _, public, seed = new_key()
        kid = f"{sub}-test"
        key_file = directory / f"{sub}.key"
        key_file.write_text(seed)
        keys[sub] = (kid, key_file)
        seeds.append(seed)
        entries.append(
            f'[[keys]]\nkid = "{kid}"\nsub = "{sub}"\npublic_key = "{public}"\n'
            f'capabilities = ["{READ}", "{WRITE}"]\n'
        )
    registry_file = directory / "registry.toml"
    registry_file.write_text("\n".join(entries))
    authenticator = sa.ServiceAuthenticator(
        audience=AUDIENCE, registry=_loaded(sa.KeyRegistry(registry_file))
    )
    policies = policies or PolicyProvider(load_policy())
    app = create_app(
        repository=repo, authenticator=authenticator, policies=policies, clock=clock,
        max_body_bytes=max_body_bytes,
    )
    world = World(TestClient(app), repo, policies, clock, directory, keys)
    world.secrets_seen.extend(seeds)
    return world


def _loaded(registry: sa.KeyRegistry) -> sa.KeyRegistry:
    registry.load_initial()
    return registry


def create_body(content: str = "a plain note", **overrides: Any) -> dict:
    body = {
        "idempotency_key": "key-" + secrets.token_hex(8),
        "content": content,
        "provenance": {"kind": "agent_memory", "source_id": "test:source"},
    }
    body.update(overrides)
    return body


def update_body(rev: int, content: str = "a revised note", **overrides: Any) -> dict:
    body = {
        "idempotency_key": "key-" + secrets.token_hex(8),
        "expected_rev": rev,
        "content": content,
        "provenance": {"kind": "agent_memory", "source_id": "test:source2"},
    }
    body.update(overrides)
    return body


def agent_path(agent: str = "nova", memory_id: str | None = None, action: str | None = None) -> str:
    path = f"/memory/agent/{agent}/records"
    if memory_id:
        path += f"/{memory_id}"
        if action:
            path += f"/{action}"
    return path


def session_path(session: str = "sess-1", memory_id: str | None = None, action: str | None = None) -> str:
    path = f"/memory/session/{session}/records"
    if memory_id:
        path += f"/{memory_id}"
        if action:
            path += f"/{action}"
    return path


def only_policy(**changes: Any) -> PolicyProvider:
    """The shipped policy with one agent's fields changed, for ceiling and limit tests."""
    from dataclasses import replace

    base = load_policy()
    agents = [replace(a, **changes) if a.agent_id == "nova" else a for a in base.agents]
    return PolicyProvider(MemoryPolicy(agents))


# --- PostgreSQL fixtures ---------------------------------------------------------------------


@dataclass(frozen=True)
class Database:
    admin_dsn: str  # superuser, on the throwaway database (tests only; never given to the service)
    service_dsn: str
    migrator_dsn: str
    name: str


def admin_dsn_or_skip() -> str:
    dsn = os.environ.get(ADMIN_ENV, "").strip()
    if not dsn:
        pytest.skip(
            f"{ADMIN_ENV} is not set: the PostgreSQL contract tests need a throwaway PostgreSQL "
            "server (see docs/memory-service.md, 'Running the contract tests')"
        )
    return dsn


def provision_database(admin: str) -> Database:
    """Roles, a fresh database and the real migrations, as the Compose init script and runner do."""
    import psycopg
    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from viharbot.memory_service.migrate import migrate

    service_pw, migrator_pw = secrets.token_hex(12), secrets.token_hex(12)
    name = "memtest_" + secrets.token_hex(6)
    with psycopg.connect(admin, autocommit=True) as conn:
        for role, password in (("memory_migrator", migrator_pw), ("memory_service", service_pw)):
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(sql.SQL(f"{verb} ROLE {role} LOGIN PASSWORD {{}}").format(sql.Literal(password)))
        conn.execute(sql.SQL("CREATE DATABASE {} OWNER memory_migrator").format(sql.Identifier(name)))
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
        conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO memory_service").format(sql.Identifier(name)))
    migrator = make_conninfo(admin, dbname=name, user="memory_migrator", password=migrator_pw)
    service = make_conninfo(admin, dbname=name, user="memory_service", password=service_pw)
    migrate(migrator)
    return Database(make_conninfo(admin, dbname=name), service, migrator, name)


def drop_database(admin: str, name: str) -> None:
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))


def truncate_all(database: Database) -> None:
    import psycopg

    with psycopg.connect(database.migrator_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE memory.records, memory.record_versions, memory.idempotency, "
            "memory.erase_requests, memory.tombstones"
        )
