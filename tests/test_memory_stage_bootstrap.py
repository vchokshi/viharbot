"""The Stage bootstrap job (docs/stage-memory-design.md item 6, ``MEMORY_STAGE_ROLE=bootstrap``).

Offline: a fake gateway stands in for the database, as ``tests/test_digitalocean_probe.py`` does
for the probe job. Nothing here contacts DigitalOcean or PostgreSQL. These tests prove the job's
LOGIC (order, idempotence, refusals, quiet logs). They do NOT prove any SQL works on DigitalOcean;
the CONNECT grant and WITH SET TRUE were validated there by the probe retest 2026-11-23, and
client-side SCRAM hashing is still untested.
"""

from __future__ import annotations

import base64
import datetime as dt
import inspect
import json
import os
import re
import secrets

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from viharbot.memory_service import stage_bootstrap as sb
from viharbot.memory_service import stage_entry as se


def make_ca_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test cluster ca")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
            .not_valid_after(now + dt.timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    return cert.public_bytes(serialization.Encoding.PEM).decode()


class FakeSession:
    def __init__(self, db: "FakeDatabase", user: str | None) -> None:
        self.db, self.user, self.closed = db, user, False

    def execute(self, sql: str):
        self.db.statements.append(sql)
        for needle, sqlstate in self.db.fail_on.items():
            if needle in sql:
                raise sb.DbError(sqlstate)
        if "FROM pg_roles" in sql:
            role = re.search(r"rolname = '([^']+)'", sql).group(1)
            return [(1,)] if role in self.db.roles else []
        if "FROM pg_database" in sql:
            return [(1,)] if self.db.database_exists else []
        if sql.startswith(("CREATE ROLE", "ALTER ROLE")):
            self.db.roles.add(re.search(r'ROLE "([^"]+)"', sql).group(1))
            return []
        if sql == "SELECT current_user":
            return [(self.user,)]
        return []

    def encrypt_password(self, role: str, password: str):
        return self.db.hash_prefix + password[::-1] if self.db.hash_prefix else None

    def close(self) -> None:
        self.closed = True


class FakeDatabase:
    """A fake gateway whose state (the roles that exist) survives between runs."""

    def __init__(self) -> None:
        self.roles: set[str] = set()
        self.statements: list[str] = []
        self.fail_on: dict[str, str] = {}
        self.connect_fail: dict[str | None, str] = {}
        self.database_exists = True
        self.hash_prefix = ""
        self.logins: list[str | None] = []
        self.conninfos: list[str] = []

    def factory(self, conninfo: str):
        self.conninfos.append(conninfo)
        return self

    def connect(self, *, user=None, password=None):
        self.logins.append(user)
        if user in self.connect_fail:
            raise sb.DbError(self.connect_fail[user])
        return FakeSession(self, user)


class Env:
    def __init__(self) -> None:
        self.ca = make_ca_pem()
        self.migrator_pw = "m-" + secrets.token_urlsafe(24)
        self.service_pw = "s-" + secrets.token_urlsafe(24)
        self.admin_pw = "a-" + secrets.token_urlsafe(24)
        self.values = {
            "MEMORY_STAGE_ROLE": "bootstrap",
            "MEMORY_ADMIN_DB_HOST": "db.cluster.example",
            "MEMORY_ADMIN_DB_PORT": "25060",
            "MEMORY_ADMIN_DB_USER": "doadmin",
            "MEMORY_ADMIN_DB_PASSWORD": self.admin_pw,
            "MEMORY_ADMIN_DB_NAME": "memory",
            "MEMORY_DB_CA_PEM": base64.b64encode(self.ca.encode()).decode(),
            "MEMORY_DB_MIGRATOR_PASSWORD": self.migrator_pw,
            "MEMORY_DB_SERVICE_PASSWORD": self.service_pw,
        }

    def secrets(self) -> list[str]:
        return [self.migrator_pw, self.service_pw, self.admin_pw]


@pytest.fixture()
def env():
    return Env()


class MigrateRecorder:
    def __init__(self, applied=("0001_memory_schema.sql",), error: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.applied, self.error = list(applied), error

    def __call__(self, conninfo: str):
        self.calls.append(conninfo)
        if self.error:
            raise self.error
        return self.applied


def run(env: Env, db: FakeDatabase, migrate=None, **overrides):
    values = {**env.values, **overrides}
    values = {k: v for k, v in values.items() if v is not None}
    return sb.run_bootstrap(values, gateway_factory=db.factory, migrate_fn=migrate or MigrateRecorder())


def code_only() -> str:
    """The module source without its docstring (which describes the untested statements in prose)."""
    return inspect.getsource(sb).split('"""', 2)[2]


def events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines()]


def steps(out: str) -> dict[str, dict]:
    return {e["step"]: e for e in events(out) if e["event"] == "memory_bootstrap_step"}


# ------------------------------------------------------------------------------ the happy path


def test_a_first_run_creates_both_roles_in_order_and_migrates_as_the_migrator(env, capsys):
    db, migrate = FakeDatabase(), MigrateRecorder()
    assert run(env, db, migrate) == sb.EXIT_OK
    sql = db.statements
    assert sql[0].startswith("SELECT 1 FROM pg_database")
    kinds = [s.split(" WITH ")[0] for s in sql if s.startswith(("CREATE ROLE", "ALTER ROLE"))]
    assert kinds == ['CREATE ROLE "memory_migrator"', 'CREATE ROLE "memory_service"']
    for line in (s for s in sql if s.startswith("CREATE ROLE")):
        assert "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE" in line
    ordered = ["REVOKE ALL ON DATABASE", 'GRANT CONNECT ON DATABASE "memory" TO "memory_service"',
               'GRANT "memory_migrator" TO CURRENT_USER WITH SET TRUE',
               'GRANT CONNECT ON DATABASE "memory" TO "memory_migrator"',
               'CREATE SCHEMA IF NOT EXISTS "memory"', 'ALTER SCHEMA "memory" OWNER TO "memory_migrator"']
    positions = [next(i for i, s in enumerate(sql) if s.startswith(o)) for o in ordered]
    assert positions == sorted(positions)
    assert db.logins == [None, "memory_service"]  # admin, then the service login check
    assert len(migrate.calls) == 1
    out = capsys.readouterr().out
    assert [e["event"] for e in events(out)][-1] == "memory_bootstrap_done"


def test_the_migrator_connection_is_verify_full_with_a_ca_file_as_the_migrator_role(env):
    db, migrate = FakeDatabase(), MigrateRecorder()
    run(env, db, migrate)
    admin_conninfo, = db.conninfos
    for conninfo in (admin_conninfo, migrate.calls[0]):
        assert "sslmode=verify-full" in conninfo and "sslrootcert=" in conninfo
        assert "host=db.cluster.example" in conninfo and "port=25060" in conninfo and "dbname=memory" in conninfo
    assert "user=doadmin" in admin_conninfo and env.admin_pw in admin_conninfo
    assert "user=memory_migrator" in migrate.calls[0] and env.migrator_pw in migrate.calls[0]
    assert env.admin_pw not in migrate.calls[0] and env.service_pw not in migrate.calls[0]


def test_the_ca_file_is_removed_when_the_job_ends(env):
    db = FakeDatabase()
    run(env, db)
    path = re.search(r"sslrootcert=(\S+)", db.conninfos[0]).group(1)
    assert not os.path.exists(path) and not os.path.exists(os.path.dirname(path))


def test_passwords_are_hashed_client_side_when_the_driver_can(env):
    db = FakeDatabase()
    db.hash_prefix = "SCRAM-SHA-256$HASHED:"
    run(env, db)
    role_sql = " ".join(s for s in db.statements if s.endswith(("'",)) and "ROLE" in s)
    assert "SCRAM-SHA-256$HASHED:" in role_sql
    assert env.migrator_pw not in role_sql and env.service_pw not in role_sql


def test_without_a_hashing_driver_the_job_still_works_with_the_plain_password(env):
    db = FakeDatabase()
    assert run(env, db) == sb.EXIT_OK
    assert any(env.migrator_pw in s for s in db.statements if s.startswith("CREATE ROLE"))


def test_a_password_with_a_quote_is_escaped_not_injected():
    assert sb.quote_literal("it's") == "'it''s'" and sb.quote_ident('a"b') == '"a""b"'


# ----------------------------------------------------------------------------- idempotence


def test_a_second_run_alters_instead_of_creating_and_changes_nothing_else(env, capsys):
    db = FakeDatabase()
    assert run(env, db) == sb.EXIT_OK
    first = [s for s in db.statements if not s.startswith(("SELECT", "CREATE ROLE", "ALTER ROLE"))]
    db.statements.clear()
    capsys.readouterr()
    assert run(env, db) == sb.EXIT_OK
    second_roles = [s.split(" WITH ")[0] for s in db.statements if "ROLE" in s]
    assert second_roles == ['ALTER ROLE "memory_migrator"', 'ALTER ROLE "memory_service"']
    assert not any(s.startswith("CREATE ROLE") for s in db.statements)
    second = [s for s in db.statements if not s.startswith(("SELECT", "CREATE ROLE", "ALTER ROLE"))]
    assert second == first  # same grants, same schema statements, all repeatable by themselves
    assert all(s.startswith(("REVOKE", "GRANT", "CREATE SCHEMA IF NOT EXISTS", "ALTER SCHEMA")) for s in second)
    assert [e["event"] for e in events(capsys.readouterr().out)][-1] == "memory_bootstrap_done"


def test_no_statement_is_a_bare_create_other_than_the_first_role_creation(env):
    db = FakeDatabase()
    db.roles = {"memory_migrator", "memory_service"}
    run(env, db)
    creates = [s for s in db.statements if re.match(r"CREATE (?!SCHEMA IF NOT EXISTS)", s)]
    assert creates == []  # every CREATE is IF NOT EXISTS or replaced by ALTER once the role exists


# ------------------------------------------- the platform grants (probe retest 2026-11-23)


def test_the_platform_grants_live_in_one_function_and_are_validated(env, capsys):
    steps_ = sb.platform_grant_steps("memory")
    assert [s.name for s in steps_] == ["grant_migrator_to_admin_set_true", "grant_connect_migrator"]
    # Probe retest 2026-11-23: both tested on DigitalOcean, so neither is flagged untested.
    assert not any(s.untested for s in steps_)
    # CONNECT is required (fatal); WITH SET TRUE is accepted but optional (non-fatal).
    assert {s.name: s.fatal for s in steps_} == {"grant_migrator_to_admin_set_true": False,
                                                 "grant_connect_migrator": True}
    db = FakeDatabase()
    run(env, db)
    reported = steps(capsys.readouterr().out)
    for name in ("grant_connect_migrator", "grant_migrator_to_admin_set_true", "create_schema"):
        assert reported[name]["untested_on_digitalocean"] is False
    # The migrator CONNECT grant and the SET grant are written only inside that function.
    source = code_only()
    isolated = inspect.getsource(sb.platform_grant_steps)
    outside = source.replace(isolated, "")
    assert "WITH SET TRUE" not in outside
    assert not re.search(r'GRANT CONNECT ON DATABASE \{db\} TO \{migrator\}', outside)
    # ALTER DATABASE ... OWNER failed on DigitalOcean without a membership grant and is not used
    assert not any("ALTER DATABASE" in statement for statement in db.statements)
    assert not any("ALTER DATABASE" in step.sql for step in steps_)
    assert not re.search(r'f?"[^"\n]*ALTER DATABASE', source)


def test_client_side_scram_hashing_is_still_flagged_untested(env, capsys):
    db = FakeDatabase()
    db.hash_prefix = "SCRAM-SHA-256$"
    assert run(env, db) == sb.EXIT_OK
    reported = steps(capsys.readouterr().out)
    assert reported["create_role_memory_migrator"]["untested_on_digitalocean"] is True
    plain = FakeDatabase()
    run(env, plain)
    assert steps(capsys.readouterr().out)["create_role_memory_migrator"]["untested_on_digitalocean"] is False


def test_a_refused_set_grant_is_recorded_and_the_job_carries_on(env, capsys):
    db = FakeDatabase()
    db.fail_on = {"WITH SET TRUE": "42601"}
    assert run(env, db) == sb.EXIT_OK
    reported = steps(capsys.readouterr().out)
    assert reported["grant_migrator_to_admin_set_true"]["outcome"] == "error"
    assert reported["grant_migrator_to_admin_set_true"]["sqlstate"] == "42601"


def test_a_refused_migrator_connect_grant_stops_the_job_before_the_migration(env, capsys):
    db, migrate = FakeDatabase(), MigrateRecorder()
    db.fail_on = {'CONNECT ON DATABASE "memory" TO "memory_migrator"': "42501"}
    assert run(env, db, migrate) == sb.EXIT_FAILED
    assert migrate.calls == []
    assert not any(s.startswith(("CREATE SCHEMA", "ALTER SCHEMA")) for s in db.statements)
    assert steps(capsys.readouterr().out)["grant_connect_migrator"]["sqlstate"] == "42501"


# ------------------------------------------------------------------------------- failures


def test_a_missing_database_fails_before_any_role_is_touched(env):
    db = FakeDatabase()
    db.database_exists = False
    assert run(env, db) == sb.EXIT_FAILED
    assert not any("ROLE" in s for s in db.statements)


def test_an_admin_connection_failure_is_a_failed_job_with_a_sqlstate_only(env, capsys):
    db = FakeDatabase()
    db.connect_fail = {None: "28P01"}
    assert run(env, db) == sb.EXIT_FAILED
    assert steps(capsys.readouterr().out)["admin_connect"]["sqlstate"] == "28P01"


def test_a_migration_failure_is_a_failed_job_and_the_service_login_is_not_checked(env, capsys):
    db = FakeDatabase()
    assert run(env, db, MigrateRecorder(error=RuntimeError(f"boom {env.service_pw}"))) == sb.EXIT_FAILED
    out = capsys.readouterr().out
    assert steps(out)["migrate"]["error_class"] == "RuntimeError"
    assert env.service_pw not in out
    assert db.logins == [None]


def test_a_service_role_that_cannot_log_in_fails_the_job(env, capsys):
    db = FakeDatabase()
    db.connect_fail = {"memory_service": "42501"}
    assert run(env, db) == sb.EXIT_FAILED
    assert steps(capsys.readouterr().out)["service_login"]["sqlstate"] == "42501"


def test_a_failing_statement_stops_the_job_in_order(env):
    db, migrate = FakeDatabase(), MigrateRecorder()
    db.fail_on = {"REVOKE ALL ON DATABASE": "42501"}
    assert run(env, db, migrate) == sb.EXIT_FAILED
    assert migrate.calls == [] and not any(s.startswith("GRANT") for s in db.statements)


# ----------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("override, fragment", [
    ({"MEMORY_DB_MIGRATOR_PASSWORD": None}, "MEMORY_DB_MIGRATOR_PASSWORD is not set"),
    ({"MEMORY_DB_SERVICE_PASSWORD": ""}, "MEMORY_DB_SERVICE_PASSWORD is not set"),
    ({"MEMORY_DB_SERVICE_PASSWORD": "short"}, "at least 16"),
    ({"MEMORY_ADMIN_DB_PASSWORD": None}, "MEMORY_ADMIN_DB_PASSWORD is required"),
    ({"MEMORY_ADMIN_DB_HOST": None}, "MEMORY_ADMIN_DB_HOST is required"),
    ({"MEMORY_ADMIN_DB_NAME": None}, "MEMORY_ADMIN_DB_NAME is required"),
    ({"MEMORY_DB_CA_PEM": None}, "verify-full with a CA file is required"),
    ({"MEMORY_DB_CA_PEM": "not a certificate"}, "MEMORY_DB_CA_PEM"),
    ({"MEMORY_MIGRATE_DB_USER": "doadmin"}, "must be memory_migrator"),
    ({"MEMORY_MIGRATE_DB_NAME": "another"}, "must equal MEMORY_ADMIN_DB_NAME"),
])
def test_bad_configuration_is_refused_before_any_connection(env, override, fragment):
    db = FakeDatabase()
    with pytest.raises(se.StageConfigError) as caught:
        run(env, db, **override)
    assert fragment in str(caught.value)
    assert db.conninfos == [] and db.statements == []
    for secret in env.secrets():
        assert secret not in str(caught.value)


def test_identical_role_passwords_are_refused(env):
    same = "x-" + secrets.token_urlsafe(24)
    with pytest.raises(se.StageConfigError, match="must differ"):
        run(env, FakeDatabase(), MEMORY_DB_MIGRATOR_PASSWORD=same, MEMORY_DB_SERVICE_PASSWORD=same)


def test_a_loopback_host_without_a_ca_is_allowed_for_local_rehearsal_only(env):
    db = FakeDatabase()
    assert run(env, db, MEMORY_ADMIN_DB_HOST="127.0.0.1", MEMORY_DB_CA_PEM=None,
               MEMORY_ADMIN_DB_SSLMODE="prefer") == sb.EXIT_OK


def test_the_migrate_variables_may_be_given_explicitly_and_must_agree(env):
    db, migrate = FakeDatabase(), MigrateRecorder()
    assert run(env, db, migrate, MEMORY_MIGRATE_DB_HOST="db.cluster.example", MEMORY_MIGRATE_DB_PORT="25060",
               MEMORY_MIGRATE_DB_NAME="memory", MEMORY_MIGRATE_DB_USER="memory_migrator") == sb.EXIT_OK
    assert "sslmode=verify-full" in migrate.calls[0]


# ------------------------------------------------------------------------------ quiet logs


def test_no_secret_or_connection_detail_is_ever_printed(env, capsys):
    ok_db = FakeDatabase()
    run(env, ok_db)
    bad_db = FakeDatabase()
    bad_db.fail_on = {"GRANT CONNECT": "42501"}
    run(env, bad_db)
    bad_db.connect_fail = {None: "28P01"}
    run(env, bad_db)
    out = capsys.readouterr()
    text = out.out + out.err
    for secret in env.secrets():
        assert secret not in text
    for fragment in ("db.cluster.example", "25060", "doadmin", "BEGIN CERTIFICATE", "sslrootcert", "postgresql://"):
        assert fragment not in text
    for line in out.out.splitlines():
        record = json.loads(line)  # one JSON object per line
        assert set(record) <= {"timestamp", "level", "agent", "event", "step", "outcome", "sqlstate",
                               "untested_on_digitalocean", "database_tls", "applied", "error_class"}


# ------------------------------------------------------------------ launcher dispatch


def test_the_launcher_dispatches_the_bootstrap_role(env):
    db, migrate = FakeDatabase(), MigrateRecorder()
    assert se.main(env.values, gateway_factory=db.factory, migrate_fn=migrate) == sb.EXIT_OK
    assert len(migrate.calls) == 1


def test_the_launcher_turns_a_bootstrap_refusal_into_exit_1(env, capsys):
    values = {**env.values, "MEMORY_DB_SERVICE_PASSWORD": "short"}
    assert se.main(values) == se.EXIT_REFUSED
    assert "refusing to start" in capsys.readouterr().err


def test_serve_still_works_and_other_roles_are_still_refused(env, capsys):
    assert se.main({"MEMORY_STAGE_ROLE": "nonsense"}) == se.EXIT_BAD_ROLE
    assert "'serve' or 'bootstrap'" in capsys.readouterr().err


def test_the_module_makes_no_claim_about_digitalocean_success_and_has_no_superuser_grant():
    source = code_only()
    assert "SUPERUSER" not in source.replace("NOSUPERUSER", "")
    assert not re.search(r"(?i)works on digitalocean|verified on digitalocean", inspect.getsource(sb))
    assert "MERGE HELD" not in inspect.getsource(sb)
    assert "probe retest 2026-11-23" in inspect.getsource(sb)
