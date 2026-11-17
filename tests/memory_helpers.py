"""Shared fixtures for the memory service tests (ADR-009, issue #40, PR 1).

Offline: no model, no gateway, no network beyond an optional PostgreSQL. Callers are authenticated
with throwaway Ed25519 keys through the real A2 verifier.

Database fixtures (selected by environment variables)
-----------------------------------------------------
VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN  the admin DSN, for example
    ``postgresql://postgres:<pw>@127.0.0.1:55432/postgres``. Unset: the database tests skip. The
    admin may be a superuser (a throwaway local container) or a non-superuser holding CREATEROLE and
    CREATEDB (a managed cluster such as DigitalOcean's ``doadmin``). For TLS use
    ``sslmode=verify-full`` and ``sslrootcert=<path to the CA file>``; the derived service and
    migrator DSNs inherit both because they connect to the same host.
VIHARBOT_MEMORY_TEST_PG_ALLOW_REMOTE=1  required when the admin host is not loopback.
VIHARBOT_MEMORY_TEST_PG_CONFIRM_EMPTY=1  also required for a remote host: the operator states the
    cluster is empty (a throwaway or pre-app cluster). It is not taken on trust: the fixture then
    checks the cluster itself and REFUSES (fails, never proceeds) if any database exists beyond the
    system ones and the ``vbtest_`` prefix, or if roles named ``memory_migrator`` or
    ``memory_service`` exist, because either means a live memory service.
VIHARBOT_MEMORY_TEST_PG_FORCE_REMOTE_GUARD=1  apply the remote rules even to a loopback host (to
    exercise the guard locally). It can only make the checks stricter.
VIHARBOT_MEMORY_TEST_PG_CONTAINER  name of a local Docker container, only for the container-restart
    test; that test skips on a remote cluster.

Isolation: each run creates its own uniquely named roles (``vbtest_m_<hex>``, ``vbtest_s_<hex>``)
and database (``vbtest_<hex>``) and, on teardown, drops only those. It never ALTERs or drops
anything that already existed, so the real ``memory_migrator``, ``memory_service`` and their
database are untouched. The migrations hard-code the service role name; the fixture applies them
from a temporary copy with that one identifier replaced by the run's service role (the shipped
files are not edited). With a non-superuser admin the fixture grants itself membership in the two
roles (INHERIT and SET, needed for the ownership checks and CREATE DATABASE ... OWNER on
PostgreSQL 16+); dropping the roles at teardown removes those memberships.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

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
ALLOW_REMOTE_ENV = "VIHARBOT_MEMORY_TEST_PG_ALLOW_REMOTE"
CONFIRM_EMPTY_ENV = "VIHARBOT_MEMORY_TEST_PG_CONFIRM_EMPTY"
FORCE_GUARD_ENV = "VIHARBOT_MEMORY_TEST_PG_FORCE_REMOTE_GUARD"
TEST_PREFIX = "vbtest_"
REAL_ROLES = ("memory_migrator", "memory_service")
SERVICE_ROLE_NAME = "memory_service"  # the identifier the shipped migrations hard-code
# Databases a fresh cluster legitimately holds (PostgreSQL's own, and DigitalOcean's defaults).
SYSTEM_DATABASES = frozenset({"postgres", "template0", "template1", "defaultdb", "_dodb"})
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
    admin_dsn: str  # the admin, on the throwaway database (tests only; never given to the service)
    service_dsn: str
    migrator_dsn: str
    name: str
    service_role: str = SERVICE_ROLE_NAME
    migrator_role: str = "memory_migrator"
    migrations_dir: Path | None = None  # the shipped migrations with the service role name swapped


# --- guard logic (pure, no database: unit-tested) ---


def _dsn_dict(dsn: str) -> dict[str, str]:
    from psycopg.conninfo import conninfo_to_dict

    return {k: str(v) for k, v in conninfo_to_dict(dsn).items() if v is not None}


def is_loopback_host(host: str) -> bool:
    """True for a unix socket, a loopback literal, or a name that resolves only to loopback.

    Anything that cannot be resolved counts as NOT loopback (fail safe).
    """
    host = host.strip()
    if not host or host.startswith("/"):  # empty host = default unix socket
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_loopback for a in addresses)


def dsn_is_remote(dsn: str, environ: Mapping[str, str] | None = None) -> bool:
    """True unless every host in the DSN is loopback (hostaddr wins over host, as in libpq)."""
    env = os.environ if environ is None else environ
    if env.get(FORCE_GUARD_ENV) == "1":
        return True
    parts = _dsn_dict(dsn)
    hosts = (parts.get("hostaddr") or parts.get("host") or "").split(",")
    return not all(is_loopback_host(h) for h in hosts)


def remote_opt_in_problems(environ: Mapping[str, str]) -> list[str]:
    """What is missing before a remote cluster may be touched (both opt-ins are required)."""
    missing = []
    if environ.get(ALLOW_REMOTE_ENV) != "1":
        missing.append(f"{ALLOW_REMOTE_ENV}=1 (explicit opt-in to a non-loopback cluster)")
    if environ.get(CONFIRM_EMPTY_ENV) != "1":
        missing.append(f"{CONFIRM_EMPTY_ENV}=1 (explicit confirmation that the cluster is empty)")
    return missing


def cluster_problems(databases: list[str], roles: list[str]) -> list[str]:
    """Why a remote cluster is NOT empty (a live memory service may use it). Empty list = proceed."""
    problems = []
    extra = sorted(d for d in databases if d not in SYSTEM_DATABASES and not d.startswith(TEST_PREFIX))
    if extra:
        problems.append(f"databases beyond the system and {TEST_PREFIX}* ones exist: {', '.join(extra)}")
    clash = sorted(set(REAL_ROLES) & set(roles))
    if clash:
        problems.append(f"roles of a live memory service exist: {', '.join(clash)}")
    return problems


def admin_privilege_problems(is_super: bool, can_create_role: bool, can_create_db: bool) -> list[str]:
    if is_super:
        return []
    return [name for name, ok in (("CREATEROLE", can_create_role), ("CREATEDB", can_create_db)) if not ok]


def derive_dsn(admin: str, *, dbname: str, user: str | None = None, password: str | None = None) -> str:
    """A DSN on the admin's host for another database or role; TLS settings are carried over."""
    from psycopg.conninfo import make_conninfo

    overrides = {"dbname": dbname}
    if user is not None:
        overrides["user"] = user
    if password is not None:
        overrides["password"] = password
    derived = make_conninfo(admin, **overrides)
    # the service and migrator connect to the same host, so they must verify it the same way
    for key in ("sslmode", "sslrootcert"):
        assert _dsn_dict(admin).get(key) == _dsn_dict(derived).get(key), f"{key} was not inherited"
    return derived


def rewrite_role_name(text: str, role: str) -> str:
    """The migration text with the hard-coded service role identifier replaced by ``role``."""
    assert re.fullmatch(r"[a-z0-9_]+", role), "the role name must be a plain lowercase identifier"
    return re.sub(rf"\b{SERVICE_ROLE_NAME}\b", role, text)


# --- provisioning ---

_checked: dict[str, tuple[str, str] | None] = {}  # admin dsn -> ("skip" | "fail", message) or None
_created: dict[str, tuple[str, ...]] = {}  # database name -> roles this process created for it
_workdirs: dict[str, Path] = {}


def cluster_is_remote() -> bool:
    return dsn_is_remote(os.environ.get(ADMIN_ENV, "").strip()) if os.environ.get(ADMIN_ENV) else False


def _check_admin(dsn: str) -> tuple[str, str] | None:
    import psycopg

    remote = dsn_is_remote(dsn)
    if remote:
        missing = remote_opt_in_problems(os.environ)
        if missing:
            return ("skip", "the admin DSN is not loopback, so this run needs " + " and ".join(missing))
    with psycopg.connect(dsn, autocommit=True, connect_timeout=15) as conn:
        is_super, can_role, can_db, who = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolname FROM pg_roles WHERE rolname = current_user"
        ).fetchone()
        missing_priv = admin_privilege_problems(is_super, can_role, can_db)
        if missing_priv:
            return ("skip", f"the admin role {who} lacks {' and '.join(missing_priv)}, which the fixture needs "
                            "to create its own roles and database")
        if remote:
            databases = [r[0] for r in conn.execute("SELECT datname FROM pg_database")]
            roles = [r[0] for r in conn.execute("SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", (list(REAL_ROLES),))]
            problems = cluster_problems(databases, roles)
            if problems:
                return ("fail", "REFUSING to run: this cluster is not empty (" + "; ".join(problems)
                        + "). The suite runs only against an empty throwaway or pre-app cluster.")
    return None


def admin_dsn_or_skip() -> str:
    """The admin DSN, after the remote-cluster guard and the privilege check have passed."""
    dsn = os.environ.get(ADMIN_ENV, "").strip()
    if not dsn:
        pytest.skip(
            f"{ADMIN_ENV} is not set: the PostgreSQL contract tests need a throwaway PostgreSQL "
            "server (see the tests/memory_helpers.py docstring)"
        )
    if dsn not in _checked:
        _checked[dsn] = _check_admin(dsn)
    verdict = _checked[dsn]
    if verdict:
        kind, message = verdict
        if kind == "skip":
            pytest.skip(message)
        pytest.fail(message, pytrace=False)
    return dsn


def skip_on_remote_cluster(reason: str) -> None:
    if cluster_is_remote():
        pytest.skip(reason)


def provision_database(admin: str) -> Database:
    """Unique roles, a unique database and the real migrations. Drops nothing it did not create."""
    import psycopg
    from psycopg import sql

    from viharbot.memory_service.migrate import discover, migrate

    tag = secrets.token_hex(6)
    name, migrator_role, service_role = f"{TEST_PREFIX}{tag}", f"{TEST_PREFIX}m_{tag}", f"{TEST_PREFIX}s_{tag}"
    service_pw, migrator_pw = secrets.token_hex(12), secrets.token_hex(12)
    _created[name] = ()  # registered before anything exists, so a failure part way still cleans up
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            is_super = conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0]
            version = int(conn.execute("SHOW server_version_num").fetchone()[0])
            for role, password in ((migrator_role, migrator_pw), (service_role, service_pw)):
                conn.execute(
                    sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                        sql.Identifier(role), sql.Literal(password)
                    )
                )
                _created[name] = (*_created[name], role)
            if not is_super:
                # A non-superuser admin must be able to SET ROLE to the owner (CREATE DATABASE ... OWNER)
                # and to inherit its privileges (own-checks, DROP DATABASE, reading the tables). PostgreSQL
                # 16+ has per-grant options; before that a plain membership grant does both. The grants
                # disappear when the roles are dropped at teardown.
                for role in (migrator_role, service_role):
                    options = " WITH INHERIT TRUE, SET TRUE" if version >= 160000 else ""
                    conn.execute(sql.SQL("GRANT {} TO CURRENT_USER" + options).format(sql.Identifier(role)))
            conn.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(name), sql.Identifier(migrator_role)))
            conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(name)))
            conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(name), sql.Identifier(service_role)))
        workdir = Path(tempfile.mkdtemp(prefix="vbtest-migrations-"))
        _workdirs[name] = workdir
        for _, filename, text in discover():
            (workdir / filename).write_text(rewrite_role_name(text, service_role), encoding="utf-8")
        migrator = derive_dsn(admin, dbname=name, user=migrator_role, password=migrator_pw)
        service = derive_dsn(admin, dbname=name, user=service_role, password=service_pw)
        migrate(migrator, workdir)
        return Database(derive_dsn(admin, dbname=name), service, migrator, name, service_role, migrator_role, workdir)
    except BaseException:
        drop_database(admin, name)
        raise


def drop_database(admin: str, target: "Database | str") -> None:
    """Drop the database and roles this process created for ``target``, and nothing else."""
    import psycopg
    from psycopg import sql

    name = target if isinstance(target, str) else target.name
    if name not in _created or not name.startswith(TEST_PREFIX):
        raise ValueError(f"refusing to drop {name!r}: this process did not create it")
    roles = _created[name]
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name)))
        for role in reversed(roles):
            assert role.startswith(TEST_PREFIX)
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
    del _created[name]
    workdir = _workdirs.pop(name, None)
    if workdir:
        shutil.rmtree(workdir, ignore_errors=True)


def truncate_all(database: Database) -> None:
    import psycopg

    with psycopg.connect(database.migrator_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE memory.records, memory.record_versions, memory.idempotency, "
            "memory.erase_requests, memory.tombstones"
        )
