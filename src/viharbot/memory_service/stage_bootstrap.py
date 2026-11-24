"""Stage bootstrap-and-migrate job (docs/stage-memory-design.md section 4.1, work item 6).

Selected by ``MEMORY_STAGE_ROLE=bootstrap`` (the launcher, ``stage_entry``, dispatches here). It is
the PRE_DEPLOY job of the Stage app: it runs before every deployment and, after a failed deployment,
the platform's rollback appears to run it again, so EVERY step is idempotent.

Probe retest 2026-11-23 (the founder's R1 to R3 on DigitalOcean, Standard PostgreSQL 17.12, as
``doadmin``) recorded the two platform-specific grants, both in ONE place, ``platform_grant_steps()``:

* ``GRANT CONNECT ON DATABASE ... TO memory_migrator`` (design 4.1 step 4): TESTED, REQUIRED. The
  migrator cannot log in to the platform-created database (SQLSTATE 42501) until it runs. Fatal.
* ``GRANT memory_migrator TO CURRENT_USER WITH SET TRUE`` (design 4.1 step 3): TESTED, accepted. It
  is only needed to hand the database to the migrator, which this job does not do, so it stays
  optional and non-fatal. ``ALTER DATABASE ... OWNER`` failed without it (42501) and is deliberately
  NOT used; a test guards its absence.

Also recorded 2026-11-23: a re-run exits 0 with the same outcomes (idempotent), and a failed
PRE_DEPLOY job is not retried by the platform but the automated rollback re-runs the previous
version's job once, so this job runs again during a rollback and MUST stay idempotent.

STILL UNTESTED on DigitalOcean: client-side SCRAM hashing of the role passwords (the plain-password
fallback stays; steps that used a client-side hash are flagged ``untested_on_digitalocean``). Every
other step is plain SQL the Compose script also runs, or was observed on DigitalOcean by the probe
(PR #326) or the retest.

Sequence (admin = the database binding's ``doadmin``, ``sslmode=verify-full`` with the cluster CA):

1. connect as admin and check the dedicated database exists;
2. create-or-alter ``memory_migrator`` and ``memory_service`` (LOGIN, no SUPERUSER, CREATEDB or
   CREATEROLE), password pre-hashed client-side as SCRAM when the driver can (SCRAM UNTESTED on DO);
3. the platform grant steps (above) and ``REVOKE ALL ON DATABASE ... FROM PUBLIC``, ``GRANT CONNECT``
   to the service role;
4. ``CREATE SCHEMA IF NOT EXISTS memory`` then ``ALTER SCHEMA memory OWNER TO memory_migrator``;
5. migrate as ``memory_migrator`` with the unchanged runner (``migrate.py``);
6. check the service role can log in (this is what proves its CONNECT grant).

Output: one JSON object per step on stdout holding the step name, outcome and SQLSTATE only. No
password, connection string, host or server error text is ever printed. Refusals are fixed messages.
The database is reached through ``DbGateway`` so the logic is tested with a fake and no database.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Protocol, Sequence

from .stage_entry import PrivateDir, StageConfigError, load_db_ca

MIGRATOR_ROLE = "memory_migrator"
SERVICE_ROLE = "memory_service"
SCHEMA = "memory"

ADMIN_PREFIX = "MEMORY_ADMIN_DB_"
MIGRATE_PREFIX = "MEMORY_MIGRATE_DB_"
MIGRATOR_PASSWORD_ENV = "MEMORY_DB_MIGRATOR_PASSWORD"
SERVICE_PASSWORD_ENV = "MEMORY_DB_SERVICE_PASSWORD"
MIN_PASSWORD_LENGTH = 16  # the Compose script enforces the same minimum

EXIT_OK = 0
EXIT_FAILED = 1


# ------------------------------------------------------------------------------- logging


def _log(level: str, event: str, **fields: Any) -> None:
    line = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": level, "agent": "memory",
            "event": event, **fields}
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


# ---------------------------------------------------------------------------- SQL quoting


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """A standard-conforming string literal. NUL is refused earlier, never quoted."""
    return "'" + value.replace("'", "''") + "'"


# ------------------------------------------------------------------------------- gateway


class DbError(Exception):
    """A database failure reduced to its SQLSTATE. The driver's message is dropped on purpose."""

    def __init__(self, sqlstate: str | None) -> None:
        super().__init__(sqlstate or "unknown")
        self.sqlstate = sqlstate


class DbSession(Protocol):
    def execute(self, sql: str) -> list[tuple[Any, ...]]: ...

    def encrypt_password(self, role: str, password: str) -> str | None:
        """SCRAM-hash a password client-side (libpq ``encrypt_password``), or None if unavailable."""
        ...

    def close(self) -> None: ...


class DbGateway(Protocol):
    def connect(self, *, user: str | None = None, password: str | None = None) -> DbSession:
        """A session as the admin (no arguments) or as the named role. Raises ``DbError``."""
        ...


class PsycopgGateway:
    """The real gateway: one autocommit psycopg connection per ``connect``, TLS settings fixed.

    ``admin_conninfo`` comes from ``conninfo_from_environment(..., strict_tls=True)`` and so is
    ``verify-full`` with ``sslrootcert`` (or loopback, local rehearsal only).
    """

    def __init__(self, admin_conninfo: str) -> None:
        self._admin = admin_conninfo

    def connect(self, *, user: str | None = None, password: str | None = None) -> DbSession:
        import psycopg
        from psycopg.conninfo import make_conninfo

        conninfo = self._admin if user is None else make_conninfo(self._admin, user=user, password=password)
        try:
            return _PsycopgSession(psycopg.connect(conninfo, autocommit=True, connect_timeout=10))
        except psycopg.Error as exc:
            raise DbError(getattr(exc, "sqlstate", None)) from None


class _PsycopgSession:
    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def execute(self, sql: str) -> list[tuple[Any, ...]]:
        import psycopg

        try:
            cursor = self._conn.execute(sql)  # type: ignore[arg-type]  # built from quoted identifiers
            return list(cursor.fetchall()) if cursor.description else []
        except psycopg.Error as exc:
            raise DbError(getattr(exc, "sqlstate", None)) from None

    def encrypt_password(self, role: str, password: str) -> str | None:
        try:
            return self._conn.pgconn.encrypt_password(password.encode(), role.encode(), b"scram-sha-256").decode()
        except Exception:  # an old libpq, or a driver without the call: fall back to plain
            return None

    def close(self) -> None:
        self._conn.close()


# ------------------------------------------------------------------------------- steps


@dataclass(frozen=True)
class Step:
    """One statement. ``fatal=False`` records a failure and carries on (informational steps)."""

    name: str
    sql: str
    fatal: bool = True
    untested: bool = False


def platform_grant_steps(database: str) -> list[Step]:
    """The two grants for a database the platform created and ``doadmin`` owns.

    Validated by the probe retest 2026-11-23 on DigitalOcean (PostgreSQL 17.12). The design's
    alternative, ``ALTER DATABASE <db> OWNER TO memory_migrator``, is deliberately NOT included.
    """
    db, migrator = quote_ident(database), quote_ident(MIGRATOR_ROLE)
    return [
        # Design 4.1 step 3. Tested (probe retest 2026-11-23): accepted. Not fatal: this job does
        # not change the database owner, so a refusal on another edition is recorded and the
        # following fatal steps decide.
        Step("grant_migrator_to_admin_set_true", f"GRANT {migrator} TO CURRENT_USER WITH SET TRUE",
             fatal=False),
        # Design 4.1 step 4: the migrator CONNECT grant. Tested (probe retest 2026-11-23): the
        # migrator login fails with 42501 until it runs, and works after. Fatal: without it nothing
        # else can work.
        Step("grant_connect_migrator", f"GRANT CONNECT ON DATABASE {db} TO {migrator}"),
    ]


def role_steps(role: str, password: str, session: DbSession) -> list[Step]:
    """Create-or-alter one login role. Password hashed client-side when the driver can (UNTESTED on DO)."""
    ident = quote_ident(role)
    secret = session.encrypt_password(role, password) or password
    exists = bool(session.execute(f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role)}"))
    verb = "ALTER" if exists else "CREATE"
    # PostgreSQL 16+: an admin without the superuser attribute is refused (42501) when ALTER ROLE
    # names that attribute at all, even to clear it. Only CREATE states it (a new role has none by
    # default, and CREATE is accepted); a re-run must not, so ALTER leaves it out.
    attrs = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" if verb == "CREATE" else "LOGIN NOCREATEDB NOCREATEROLE"
    hashed = secret != password  # a client-side SCRAM hash was used: not yet tried on DigitalOcean
    return [Step(f"{verb.lower()}_role_{role}", f"{verb} ROLE {ident} WITH {attrs} PASSWORD {quote_literal(secret)}",
                 untested=hashed)]


def database_steps(database: str) -> list[Step]:
    """Everything the Compose init script also does, plus the schema (all repeatable)."""
    db, service, migrator = quote_ident(database), quote_ident(SERVICE_ROLE), quote_ident(MIGRATOR_ROLE)
    schema = quote_ident(SCHEMA)
    return [
        Step("revoke_public_connect", f"REVOKE ALL ON DATABASE {db} FROM PUBLIC"),
        Step("grant_connect_service", f"GRANT CONNECT ON DATABASE {db} TO {service}"),
        *platform_grant_steps(database),
        # CREATE SCHEMA worked on DigitalOcean without AUTHORIZATION (PR #326); owner then changed.
        Step("create_schema", f"CREATE SCHEMA IF NOT EXISTS {schema}"),
        Step("alter_schema_owner", f"ALTER SCHEMA {schema} OWNER TO {migrator}"),
    ]


# -------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class BootstrapConfig:
    database: str
    migrator_password: str
    service_password: str
    admin_conninfo: str
    migrate_conninfo: str


def _check_password(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value.strip():
        raise StageConfigError(f"{name} is not set or is empty")
    if len(value) < MIN_PASSWORD_LENGTH:
        raise StageConfigError(f"{name} must be at least {MIN_PASSWORD_LENGTH} characters")
    if "\x00" in value:
        raise StageConfigError(f"{name} contains a NUL character")
    return value


def load_config(environ: Mapping[str, str], ca_path: str | None) -> BootstrapConfig:
    """Validate variables and build both connection strings. Raises ``StageConfigError``."""
    from ..service_auth import ServiceAuthConfigError
    from .app import conninfo_from_environment

    migrator_pw = _check_password(environ, MIGRATOR_PASSWORD_ENV)
    service_pw = _check_password(environ, SERVICE_PASSWORD_ENV)
    if migrator_pw == service_pw:
        raise StageConfigError("the migrator and service passwords must differ")
    database = (environ.get(ADMIN_PREFIX + "NAME") or "").strip()
    try:
        admin = conninfo_from_environment(environ, prefix=ADMIN_PREFIX, sslrootcert=ca_path, strict_tls=True)
        # The migrator connects to the same host, port and database; only user and password differ.
        migrate_env = {
            **{k: v for k, v in environ.items() if k.startswith(MIGRATE_PREFIX)},
            MIGRATE_PREFIX + "PASSWORD": migrator_pw,
        }
        for name in ("HOST", "PORT", "NAME"):
            migrate_env.setdefault(MIGRATE_PREFIX + name, environ.get(ADMIN_PREFIX + name, ""))
        if migrate_env.setdefault(MIGRATE_PREFIX + "USER", MIGRATOR_ROLE) != MIGRATOR_ROLE:
            raise StageConfigError(f"{MIGRATE_PREFIX}USER must be {MIGRATOR_ROLE}")
        migrate = conninfo_from_environment(migrate_env, prefix=MIGRATE_PREFIX, sslrootcert=ca_path, strict_tls=True)
    except ServiceAuthConfigError as error:  # fixed text naming a variable, never a value
        raise StageConfigError(str(error)) from None
    if migrate_env[MIGRATE_PREFIX + "NAME"].strip() != database:
        raise StageConfigError(f"{MIGRATE_PREFIX}NAME must equal {ADMIN_PREFIX}NAME")
    return BootstrapConfig(database, migrator_pw, service_pw, admin, migrate)


# --------------------------------------------------------------------------------- run


def _run_step(session: DbSession, step: Step) -> bool:
    try:
        session.execute(step.sql)
    except DbError as error:
        _log("error" if step.fatal else "warning", "memory_bootstrap_step", step=step.name, outcome="error",
             sqlstate=error.sqlstate, untested_on_digitalocean=step.untested)
        return not step.fatal
    _log("info", "memory_bootstrap_step", step=step.name, outcome="ok", untested_on_digitalocean=step.untested)
    return True


def _default_migrate(conninfo: str) -> list[str]:
    from .migrate import migrate

    return migrate(conninfo)


def run_bootstrap(
    environ: Mapping[str, str],
    *,
    gateway_factory: Callable[[str], DbGateway] = PsycopgGateway,
    migrate_fn: Callable[[str], Sequence[str]] = _default_migrate,
) -> int:
    """Run the whole job. Returns an exit code; configuration refusals raise ``StageConfigError``."""
    ca_pem = load_db_ca(environ)
    with PrivateDir("memory-bootstrap-") as private:
        ca_path = str(private.write("db-ca.pem", ca_pem)) if ca_pem else None
        config = load_config(environ, ca_path)
        gateway = gateway_factory(config.admin_conninfo)
        _log("info", "memory_bootstrap_start", database_tls="verify-full" if ca_path else "loopback-development")

        try:
            admin = gateway.connect()
        except DbError as error:
            _log("error", "memory_bootstrap_step", step="admin_connect", outcome="error", sqlstate=error.sqlstate)
            return EXIT_FAILED
        try:
            try:
                found = admin.execute(f"SELECT 1 FROM pg_database WHERE datname = {quote_literal(config.database)}")
            except DbError as error:
                _log("error", "memory_bootstrap_step", step="database_exists", outcome="error", sqlstate=error.sqlstate)
                return EXIT_FAILED
            if not found:
                _log("error", "memory_bootstrap_step", step="database_exists", outcome="missing")
                return EXIT_FAILED
            steps: list[Step] = []
            try:
                steps += role_steps(MIGRATOR_ROLE, config.migrator_password, admin)
                steps += role_steps(SERVICE_ROLE, config.service_password, admin)
            except DbError as error:
                _log("error", "memory_bootstrap_step", step="role_lookup", outcome="error", sqlstate=error.sqlstate)
                return EXIT_FAILED
            steps += database_steps(config.database)
            for step in steps:
                if not _run_step(admin, step):
                    return EXIT_FAILED
        finally:
            admin.close()

        try:
            applied = list(migrate_fn(config.migrate_conninfo))
        except Exception as error:  # class name only: a driver message may carry connection details
            _log("error", "memory_bootstrap_step", step="migrate", outcome="error", error_class=type(error).__name__,
                 sqlstate=getattr(error, "sqlstate", None))
            return EXIT_FAILED
        _log("info", "memory_bootstrap_step", step="migrate", outcome="ok", applied=applied)

        try:
            session = gateway.connect(user=SERVICE_ROLE, password=config.service_password)
        except DbError as error:
            _log("error", "memory_bootstrap_step", step="service_login", outcome="error", sqlstate=error.sqlstate)
            return EXIT_FAILED
        try:
            who = session.execute("SELECT current_user")
        finally:
            session.close()
        if not who or who[0][0] != SERVICE_ROLE:
            _log("error", "memory_bootstrap_step", step="service_login", outcome="wrong_user")
            return EXIT_FAILED
        _log("info", "memory_bootstrap_step", step="service_login", outcome="ok")
    _log("info", "memory_bootstrap_done", outcome="ok")
    return EXIT_OK

