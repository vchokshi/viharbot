"""The migration runner must work when the schema exists and the migrator lacks database CREATE.

PostgreSQL checks the CREATE privilege on the database before it honours IF NOT EXISTS, so
``CREATE SCHEMA IF NOT EXISTS memory`` fails for a migrator that owns the schema but not the
database (DigitalOcean Managed PostgreSQL; founder decision N6, docs/stage-memory-design.md).
Database tests are selected by VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN and skipped without it. They
use their own randomly named roles and never alter ``memory_migrator`` or ``memory_service``.
"""

from __future__ import annotations

import hashlib
import secrets
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
import pytest
from memory_helpers import admin_dsn_or_skip, drop_database
from psycopg import errors as pg_errors
from psycopg import sql
from psycopg.conninfo import make_conninfo

from viharbot.memory_service.migrate import discover, migrate, skip_schema_create

ALL_MIGRATIONS = [name for _, name, _ in discover()]


# --- database-free unit tests ----------------------------------------------------------------


def test_skip_schema_create_comments_only_the_exact_statement():
    text = discover()[0][2]
    assert "CREATE SCHEMA IF NOT EXISTS memory;" in text
    out = skip_schema_create(text)
    assert "CREATE SCHEMA" not in out.replace("-- schema already exists: CREATE SCHEMA skipped", "")
    assert len(out.splitlines()) == len(text.splitlines())  # one line replaced, nothing else
    assert out.count("CREATE TABLE") == text.count("CREATE TABLE")


def test_skip_schema_create_leaves_other_schema_statements_alone():
    text = "CREATE SCHEMA IF NOT EXISTS other;\nSELECT 'CREATE SCHEMA IF NOT EXISTS memory;';\n"
    assert skip_schema_create(text) == text


def test_skip_schema_create_is_a_no_op_on_text_without_the_statement():
    assert skip_schema_create("SELECT 1;\n") == "SELECT 1;\n"


# --- database tests --------------------------------------------------------------------------


@dataclass(frozen=True)
class Env:
    admin: str
    db: str
    role: str
    dsn: str  # the migrator, on the throwaway database


def _ensure_service_role(conn: psycopg.Connection) -> None:
    # Migration 0001 requires this role to exist. Created only when missing; never altered.
    if not conn.execute("SELECT 1 FROM pg_roles WHERE rolname = 'memory_service'").fetchone():
        conn.execute(
            sql.SQL("CREATE ROLE memory_service LOGIN PASSWORD {}").format(sql.Literal(secrets.token_hex(12)))
        )


@contextmanager
def _env(*, owns_database: bool, owns_schema: bool):
    """A throwaway database and a randomly named migrator with no database-level CREATE.

    owns_database True is the Compose shape (the migrator owns the database, no schema yet).
    owns_schema True is the Stage shape (the admin made the schema and handed it to the migrator).
    """
    admin = admin_dsn_or_skip()
    suffix = secrets.token_hex(5)
    role, db, password = f"migtest_role_{suffix}", f"migtest_db_{suffix}", secrets.token_hex(12)
    with psycopg.connect(admin, autocommit=True) as conn:
        _ensure_service_role(conn)
        conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))
        create = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db))
        if owns_database:
            create = sql.SQL("CREATE DATABASE {} OWNER {}").format(sql.Identifier(db), sql.Identifier(role))
        conn.execute(create)
        conn.execute(sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(db)))
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO memory_service, {}").format(
                sql.Identifier(db), sql.Identifier(role)
            )
        )
    if owns_schema:
        with psycopg.connect(make_conninfo(admin, dbname=db), autocommit=True) as conn:
            conn.execute("CREATE SCHEMA memory")
            conn.execute(sql.SQL("ALTER SCHEMA memory OWNER TO {}").format(sql.Identifier(role)))
    try:
        yield Env(admin, db, role, make_conninfo(admin, dbname=db, user=role, password=password))
    finally:
        drop_database(admin, db)
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))


def _applied(env: Env) -> list[str]:
    with psycopg.connect(env.dsn) as conn:
        return [r[0] for r in conn.execute("SELECT name FROM memory.schema_migrations ORDER BY version")]


def test_a_migrator_that_owns_the_schema_but_not_the_database_can_migrate():
    with _env(owns_database=False, owns_schema=True) as env:
        with psycopg.connect(env.dsn) as conn:  # the precondition this test is about
            assert conn.execute(
                "SELECT has_database_privilege(current_user, current_database(), 'CREATE')"
            ).fetchone() == (False,)
        assert migrate(env.dsn) == ALL_MIGRATIONS
        assert _applied(env) == ALL_MIGRATIONS
        with psycopg.connect(env.dsn) as conn:  # recorded over the ORIGINAL file text
            sums = [r[0] for r in conn.execute("SELECT checksum FROM memory.schema_migrations ORDER BY version")]
        assert sums == [hashlib.sha256(text.encode()).hexdigest() for _, _, text in discover()]


def test_the_compose_shape_still_creates_the_schema_and_applies_everything():
    with _env(owns_database=True, owns_schema=False) as env:
        assert migrate(env.dsn) == ALL_MIGRATIONS
        assert _applied(env) == ALL_MIGRATIONS
        with psycopg.connect(env.dsn) as conn:
            owner = conn.execute(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'memory'"
            ).fetchone()
        assert owner == (env.role,)


@pytest.mark.parametrize("owns_database,owns_schema", [(True, False), (False, True)])
def test_running_migrate_twice_is_a_no_op_the_second_time(owns_database, owns_schema):
    with _env(owns_database=owns_database, owns_schema=owns_schema) as env:
        assert migrate(env.dsn) == ALL_MIGRATIONS
        assert migrate(env.dsn) == []
        assert _applied(env) == ALL_MIGRATIONS


def test_the_migrator_without_database_create_still_cannot_create_another_schema():
    with _env(owns_database=False, owns_schema=True) as env:
        migrate(env.dsn)
        with psycopg.connect(env.dsn, autocommit=True) as conn:
            with pytest.raises(pg_errors.InsufficientPrivilege, match="permission denied for database"):
                conn.execute("CREATE SCHEMA another_schema")
        # and the runner has no path that creates one: the missing-schema case needs database CREATE
        with _env(owns_database=False, owns_schema=False) as bare:
            with pytest.raises(pg_errors.InsufficientPrivilege):
                migrate(bare.dsn)


def test_a_database_that_already_applied_the_migrations_is_unaffected():
    with _env(owns_database=True, owns_schema=False) as env:
        assert migrate(env.dsn) == ALL_MIGRATIONS  # the Compose-era history, recorded with checksums
        with psycopg.connect(env.dsn) as conn:
            before = conn.execute(
                "SELECT version, name, checksum FROM memory.schema_migrations ORDER BY version"
            ).fetchall()
        # The runner now takes the schema-exists path and must neither re-run nor re-checksum.
        assert migrate(env.dsn) == []
        with psycopg.connect(env.dsn) as conn:
            after = conn.execute(
                "SELECT version, name, checksum FROM memory.schema_migrations ORDER BY version"
            ).fetchall()
        assert after == before
