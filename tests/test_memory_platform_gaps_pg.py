"""PostgreSQL contract tests for platform properties the earlier contract tests did not pin (issue #40).

Selected by VIHARBOT_MEMORY_TEST_PG_ADMIN_DSN like the other database tests and skipped without
it. They add: schema and table ownership, the exact service-role privilege matrix, the database's
own CHECK constraints (the last line of defense behind the service), migration atomicity and
serialization, and the exact expiry boundary.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import psycopg
import pytest
from memory_helpers import T0, admin_dsn_or_skip, agent_path, build_world, create_body, drop_database, provision_database
from psycopg import errors as pg_errors

from viharbot.memory_service.migrate import discover, migrate

TABLES = ("records", "record_versions", "idempotency", "erase_requests", "tombstones")
EXPECTED_SERVICE_PRIVILEGES = {
    "records": {"SELECT", "INSERT", "UPDATE", "DELETE"},
    "record_versions": {"SELECT", "INSERT", "DELETE"},
    "idempotency": {"SELECT", "INSERT", "UPDATE"},
    "erase_requests": {"SELECT", "INSERT", "UPDATE"},
    "tombstones": {"SELECT", "INSERT"},
}
ALL_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


@pytest.fixture(scope="module")
def database():
    admin = admin_dsn_or_skip()
    db = provision_database(admin)
    yield db
    drop_database(admin, db)


def admin_rows(database, sql, params=()):
    with psycopg.connect(database.admin_dsn, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall()


# --- ownership and privileges ------------------------------------------------------------------


def test_the_migrator_owns_the_schema_and_every_table_and_the_service_role_owns_nothing(database):
    schema_owner = admin_rows(
        database, "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = 'memory'"
    )
    assert schema_owner == [(database.migrator_role,)]
    owners = admin_rows(
        database,
        "SELECT c.relname, pg_get_userbyid(c.relowner) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'memory' AND c.relkind IN ('r', 'p', 'S', 'v', 'm', 'f')",
    )
    assert owners, "the memory schema holds relations"
    assert {owner for _, owner in owners} == {database.migrator_role}
    assert {name for name, _ in owners} >= set(TABLES) | {"schema_migrations"}
    # the service role owns no database object of any kind
    assert admin_rows(
        database,
        "SELECT count(*) FROM pg_shdepend d JOIN pg_roles r ON r.oid = d.refobjid "
        "WHERE r.rolname = %s AND d.deptype = 'o' AND d.dbid = (SELECT oid FROM pg_database WHERE datname = current_database())",
        (database.service_role,),
    ) == [(0,)]


def test_the_service_role_holds_exactly_the_documented_privileges(database):
    granted: dict[str, set[str]] = {}
    for table in (*TABLES, "schema_migrations"):
        granted[table] = {
            privilege for privilege in ALL_PRIVILEGES
            if admin_rows(
                database, "SELECT has_table_privilege(%s, %s, %s)",
                (database.service_role, f"memory.{table}", privilege),
            ) == [(True,)]
        }
    assert granted == {**EXPECTED_SERVICE_PRIVILEGES, "schema_migrations": set()}
    schema_privileges = {
        privilege for privilege in ("USAGE", "CREATE")
        if admin_rows(
            database, "SELECT has_schema_privilege(%s, 'memory', %s)", (database.service_role, privilege)
        ) == [(True,)]
    }
    assert schema_privileges == {"USAGE"}


def test_public_has_no_access_to_the_memory_schema(database):
    assert admin_rows(
        database,
        "SELECT count(*) FROM pg_namespace n, aclexplode(n.nspacl) a WHERE n.nspname = 'memory' AND a.grantee = 0",
    ) == [(0,)]
    for table in (*TABLES, "schema_migrations"):
        assert admin_rows(
            database,
            "SELECT count(*) FROM pg_class c, aclexplode(c.relacl) a WHERE c.oid = %s::regclass AND a.grantee = 0",
            (f"memory.{table}",),
        ) == [(0,)]


# --- the database's own constraints ------------------------------------------------------------

RECORD = (
    "INSERT INTO memory.records (memory_id, memory_type, scope_key, owner_agent_id, classification, lifecycle, "
    "rev, version, consent_basis, consent_reference, retention_until, expires_at, created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, %s, %s, %s, %s, %s, %s)"
)
ID = "00000000-0000-7000-8000-00000000c001"


def record_row(**overrides):
    row = {
        "memory_id": ID, "memory_type": "agent_private", "scope_key": "nova", "owner_agent_id": "nova",
        "classification": "internal", "lifecycle": "active", "rev": 1, "consent_basis": "not_applicable",
        "consent_reference": None, "retention_until": T0 + timedelta(days=1), "expires_at": None,
        "created_at": T0, "updated_at": T0,
    }
    row.update(overrides)
    return tuple(row.values())


@pytest.mark.parametrize(
    "overrides",
    [
        {"memory_type": "session", "expires_at": None},  # a session record must expire
        {"expires_at": T0 + timedelta(hours=1)},  # an agent record must not carry expires_at
        {"scope_key": "scout"},  # an agent-private scope is its owner
        {"consent_basis": "consent_recorded", "consent_reference": None},
        {"classification": "secret"},
        {"lifecycle": "deleted"},
        {"rev": 0},
        {"memory_type": "engagement"},
        {"owner_agent_id": None},
    ],
)
def test_the_database_refuses_a_record_row_that_breaks_the_model(database, overrides):
    with psycopg.connect(database.admin_dsn, autocommit=True) as conn:
        with pytest.raises((pg_errors.CheckViolation, pg_errors.NotNullViolation)):
            conn.execute(RECORD, record_row(**overrides))
        assert conn.execute("SELECT count(*) FROM memory.records WHERE memory_id = %s", (ID,)).fetchone() == (0,)


def test_the_database_bounds_content_at_65536_bytes_and_refuses_empty_content(database):
    ok_id = "00000000-0000-7000-8000-00000000c002"
    with psycopg.connect(database.admin_dsn, autocommit=False) as conn:
        conn.execute(RECORD, record_row(memory_id=ok_id))
        insert = (
            "INSERT INTO memory.record_versions (memory_id, version, content, prov_kind, prov_source_id, "
            "prov_author, created_at) VALUES (%s, %s, %s, 'agent_memory', 's', 'nova', %s)"
        )
        conn.execute(insert, (ok_id, 1, "x" * 65536, T0))
        for version, content in ((2, ""), (3, "x" * 65537), (4, "é" * 32769)):
            with pytest.raises(pg_errors.CheckViolation):
                with conn.transaction():
                    conn.execute(insert, (ok_id, version, content, T0))
        with pytest.raises(pg_errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(insert, ("00000000-0000-7000-8000-00000000c003", 1, "orphan", T0))
        conn.rollback()


# --- migrations: atomic, serialized -------------------------------------------------------------


def _copy_with(database, directory, name, text):
    for _, filename, body in discover(database.migrations_dir):
        (directory / filename).write_text(body, encoding="utf-8")
    (directory / name).write_text(text, encoding="utf-8")


def test_a_failing_migration_rolls_back_its_ddl_and_its_bookkeeping_row_and_frees_the_lock(database, tmp_path):
    _copy_with(
        database, tmp_path, "0003_broken.sql",
        "CREATE TABLE memory.half_applied (x int);\nSELECT 1 / 0;\n",
    )
    with pytest.raises(psycopg.Error):
        migrate(database.migrator_dsn, tmp_path)
    assert admin_rows(database, "SELECT to_regclass('memory.half_applied')") == [(None,)]
    assert admin_rows(database, "SELECT version FROM memory.schema_migrations ORDER BY version") == [(1,), (2,)]
    # the advisory lock was released: another session can take it at once
    with psycopg.connect(database.migrator_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT pg_try_advisory_lock(7040001)").fetchone() == (True,)
        conn.execute("SELECT pg_advisory_unlock(7040001)")
    # a corrected file under a new number applies cleanly afterwards
    (tmp_path / "0003_broken.sql").unlink()
    (tmp_path / "0003_fixed.sql").write_text("CREATE TABLE memory.fixed_later (x int);\n", encoding="utf-8")
    assert migrate(database.migrator_dsn, tmp_path) == ["0003_fixed.sql"]
    with psycopg.connect(database.migrator_dsn, autocommit=True) as conn:
        conn.execute("DROP TABLE memory.fixed_later")
        conn.execute("DELETE FROM memory.schema_migrations WHERE version = 3")


def test_two_runners_started_together_apply_a_pending_migration_exactly_once(database, tmp_path):
    _copy_with(
        database, tmp_path, "0003_slow.sql",
        "SELECT pg_sleep(1.5);\nCREATE TABLE memory.slow_once (x int);\n",
    )
    barrier = threading.Barrier(2)

    def run():
        barrier.wait()
        return migrate(database.migrator_dsn, tmp_path)

    try:
        with ThreadPoolExecutor(2) as pool:
            results = [future.result(timeout=30) for future in [pool.submit(run), pool.submit(run)]]
        assert sorted(results, key=len) == [[], ["0003_slow.sql"]]
        assert admin_rows(database, "SELECT count(*) FROM memory.schema_migrations WHERE version = 3") == [(1,)]
    finally:
        with psycopg.connect(database.migrator_dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS memory.slow_once")
            conn.execute("DELETE FROM memory.schema_migrations WHERE version = 3")


def test_a_gap_or_reordered_migration_set_is_refused_before_anything_runs(database, tmp_path):
    from viharbot.memory_service.migrate import MigrationError

    (tmp_path / "0002_only.sql").write_text("SELECT 1;\n", encoding="utf-8")
    with pytest.raises(MigrationError):
        migrate(database.migrator_dsn, tmp_path)


# --- the exact expiry boundary -------------------------------------------------------------------


def test_a_record_is_unserved_at_the_exact_instant_of_its_expiry(tmp_path, database):
    from memory_helpers import session_path
    from viharbot.memory_service.postgres import PostgresMemoryRepository

    repo = PostgresMemoryRepository(database.service_dsn)
    world = build_world(tmp_path, repo)
    created = world.call("nova", "POST", session_path("edge"), create_body(ttl_seconds=60)).json()["memory_id"]
    world.clock.advance(seconds=59, microseconds=999999)
    assert world.call("nova", "GET", session_path("edge", created)).status_code == 200
    world.clock.advance(microseconds=1)  # now == expires_at: expired
    assert world.call("nova", "GET", session_path("edge", created)).status_code == 404
    assert world.call("nova", "GET", session_path("edge")).json()["records"] == []
    agent_id = world.call("nova", "POST", agent_path(), create_body(retention_until=(T0 + timedelta(days=2)).isoformat()))
    assert agent_id.status_code == 201
    world.clock.now = T0 + timedelta(days=2)
    assert world.call("nova", "GET", agent_path("nova", agent_id.json()["memory_id"])).status_code == 404


# --- a lock wait that times out is a generic 503, changes nothing, and a retry then succeeds -----


def test_a_lock_timeout_is_a_generic_503_that_leaves_the_record_untouched_and_the_retry_succeeds(tmp_path, database):
    from memory_helpers import truncate_all, update_body
    from viharbot.memory_service.postgres import PostgresMemoryRepository

    truncate_all(database)
    repo = PostgresMemoryRepository(database.service_dsn, lock_timeout_ms=300)
    world = build_world(tmp_path, repo)
    created = world.call("nova", "POST", agent_path(), create_body("original")).json()["memory_id"]
    holder = psycopg.connect(database.migrator_dsn, autocommit=False)  # a competing transaction holds the row
    try:
        holder.execute("SELECT 1 FROM memory.records WHERE memory_id = %s FOR UPDATE", (created,))
        body = update_body(1, "contended")
        response = world.call("nova", "POST", agent_path("nova", created, "update"), body)
        assert response.status_code == 503 and response.json() == {"detail": "temporarily unavailable"}
    finally:
        holder.rollback()
        holder.close()
    state = admin_rows(database, "SELECT rev, version FROM memory.records WHERE memory_id = %s", (created,))
    assert state == [(1, 1)]
    assert admin_rows(
        database, "SELECT count(*) FROM memory.idempotency WHERE idempotency_key = %s", (body["idempotency_key"],)
    ) == [(0,)]
    retry = world.call("nova", "POST", agent_path("nova", created, "update"), body)
    assert retry.status_code == 200 and retry.json()["rev"] == 2
    again = world.call("nova", "POST", agent_path("nova", created, "update"), body)
    assert again.headers["idempotent-replay"] == "true" and again.json() == retry.json()
    assert admin_rows(database, "SELECT rev, version FROM memory.records WHERE memory_id = %s", (created,)) == [(2, 2)]
