"""probe-job: the PRE_DEPLOY job (RUNBOOK facts 1, 5 and 6). THROWAWAY.

What it does, in order, each part reported in ONE JSON summary line on stdout:
  1. Reports which database-related environment variable NAMES are present, and the parsed
     connection information: host redacted, port, database name, sslmode parameter. Never a
     password, a user name or a full URL.
  2. Database TLS (fact 6): server version, whether `sslmode=require` connects, whether
     `verify-full` connects with the CA certificate in PROBE_DB_CA_PEM (and `verify-ca`, and
     `sslrootcert=system`, as information), and whether the connection really used TLS.
  3. Role bootstrap (fact 1) against a scratch schema, mirroring deploy/postgres/init/
     10-memory-roles.sh: CREATE ROLE ... LOGIN PASSWORD for a migrator and a service role, schema
     ownership, REVOKE/GRANT, default privileges, SET ROLE checks, a "service role holds no DDL
     rights" check and a real login as each new role. Each step records ok/error, SQLSTATE and the
     scrubbed error text, and whether the outcome was the expected one. Throwaway role passwords
     are generated per run in memory and never printed.
  4. Writes a marker row with a timestamp (fact 5, ordering). A second run finds the roles
     already present and ALTERs their passwords instead (re-run safety).
  5. Exits 0, unless PROBE_JOB_FAIL=true, which writes the marker (job_mode = fail) and exits 3
     so the founder can confirm a failing pre-deploy job blocks the deployment.
Probe outcomes never change the exit code (a failed CREATE ROLE is data, not a job failure); only
PROBE_JOB_FAIL and an unexpected crash do.

Retest additions (RUNBOOK section 8b, 2026-11-12 follow-up), appended AFTER the original steps:
  R2. Migrator privileges on a platform-created database: GRANT CONNECT then a real migrator
      login; `ALTER DATABASE ... OWNER TO` in two SCRATCH databases (without and with a
      membership grant; the main database's owner is never altered); `CREATE SCHEMA IF NOT
      EXISTS <existing schema>` as the migrator without CREATE on the database; the plain
      existence check; and that no other schema can be created. All idempotent.
  R3. Every marker row carries PROBE_JOB_TAG, the job mode, a random per-attempt id, the start
      time and any non-secret platform identifiers found in the environment, so a retry of a
      failing job can be told from a rollback re-running the previous version's job.

Environment: PROBE_DB_URL (preferred) or DATABASE_URL, PROBE_DB_CA_PEM (optional), PROBE_JOB_FAIL,
PROBE_JOB_TAG (a plain label from the spec).
The database is reached through `DbGateway`, so the logic is tested with a fake and no database.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from probe_common import (
    ConnectionInfo,
    ConnectionInfoError,
    DbError,
    DbGateway,
    DbSession,
    PemError,
    PsycopgGateway,
    TempSecretFiles,
    decode_pem_variable,
    env_names,
    is_sensitive_name,
    log_json,
    parse_connection_info,
    quote_ident,
    quote_literal,
    scrub,
    scrub_tree,
    utc_now_iso,
)

MIGRATOR = "probe_migrator"
SERVICE = "probe_service"
SCHEMA = "probe_scratch"
EXIT_FAIL_MODE = 3
DB_NAME_HINTS = ("DATABASE", "POSTGRES", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE", "PGSSL", "DB_")
EXPECTED_VARIABLES = ("PROBE_DB_URL", "DATABASE_URL", "PROBE_DB_HOSTNAME", "PROBE_DB_PORT",
                      "PROBE_DB_USERNAME", "PROBE_DB_CA_PEM", "PROBE_JOB_FAIL", "PROBE_JOB_TAG")
SCRATCH_DB_PREFIX = "probe_scratch_db_"
OTHER_SCHEMA = "probe_other_schema"
# Names that may carry a deployment, instance or run identifier (retest R3). The job lists every
# matching name it finds; it records a value only when the name is not sensitive and the value
# is a plain identifier (see `platform_identifiers`).
PLATFORM_ID_HINTS = ("DEPLOYMENT", "APP_ID", "INSTANCE", "COMPONENT", "JOB_", "_JOB", "RUN_ID",
                     "BUILD", "REVISION", "COMMIT", "HOSTNAME", "POD", "CONTAINER", "REPLICA",
                     "ROLLBACK", "ATTEMPT", "RETRY")
_TAG_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")
_PLAIN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TOKEN_LIKE_RE = re.compile(r"[A-Za-z0-9_-]{32,}")


def variable_report(environ: Mapping[str, str]) -> dict[str, Any]:
    """Which expected variable names are present, and every database-looking name. Names only."""
    return {
        "expected_present": {name: bool(environ.get(name)) for name in EXPECTED_VARIABLES},
        "database_like_names": [n for n in env_names(environ)
                                if any(h in n.upper() for h in DB_NAME_HINTS)],
    }


def clean_tag(raw: str | None) -> str:
    """The PROBE_JOB_TAG label: a plain token, or a fixed marker for absent/odd values."""
    if raw is None or not raw.strip():
        return "(none)"
    text = raw.strip()
    return text if _TAG_RE.fullmatch(text) else "(invalid)"


def platform_identifiers(environ: Mapping[str, str]) -> dict[str, Any]:
    """Non-secret deployment, instance or run identifiers the platform put in the environment.

    Lists the NAMES of every variable that looks like an identifier. A value is recorded only if
    the name is not sensitive, is not one of the probe's own PROBE_* variables, and the value is a
    short plain identifier (a UUID, or letters, digits, dot, dash, underscore; never a URL, and
    never a long token-like string). Anything else is listed under `withheld` by name only.
    """
    names: list[str] = []
    values: dict[str, str] = {}
    withheld: list[str] = []
    for name in sorted(environ):
        upper = name.upper()
        if upper.startswith("PROBE_") or is_sensitive_name(name):
            continue
        if not any(hint in upper for hint in PLATFORM_ID_HINTS):
            continue
        names.append(name)
        value = environ[name].strip()
        if _UUID_RE.fullmatch(value) or (
                _PLAIN_ID_RE.fullmatch(value) and not _TOKEN_LIKE_RE.fullmatch(value)):
            values[name] = value
        else:
            withheld.append(name)
    return {"names_found": names, "values": values, "withheld": withheld}


def build_attempt(environ: Mapping[str, str]) -> dict[str, Any]:
    """Identity of THIS run of the job: tag, mode, random attempt id, start time, platform ids."""
    mode = "fail" if (environ.get("PROBE_JOB_FAIL", "").lower() in ("1", "true", "yes")) else "normal"
    return {"job_tag": clean_tag(environ.get("PROBE_JOB_TAG")), "job_mode": mode,
            "attempt_id": secrets.token_hex(8), "started_at": utc_now_iso(),
            "platform_identifiers": platform_identifiers(environ)}


# ------------------------------------------------------------------------------ TLS checks


def _tls_check(name: str, gateway: DbGateway, sslmode: str, sslrootcert: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {"check": name, "sslmode": sslmode, "outcome": "error"}
    try:
        session = gateway.connect(sslmode=sslmode, sslrootcert=sslrootcert)
    except DbError as exc:
        result.update(sqlstate=exc.sqlstate, error=exc.message)
        return result
    try:
        result["outcome"] = "ok"
        try:
            result["server_version"] = session.execute("SHOW server_version")[0][0]
            rows = session.execute(
                "SELECT ssl, version FROM pg_stat_ssl WHERE pid = pg_backend_pid()")
            if rows:
                result["tls_in_use"], result["tls_version"] = bool(rows[0][0]), rows[0][1]
        except DbError as exc:
            result["note"] = f"connected, but a follow-up query failed: {exc.message}"
    finally:
        session.close()
    return result


def run_tls_checks(gateway: DbGateway, ca_path: str | None) -> list[dict[str, Any]]:
    checks = [_tls_check("sslmode_require", gateway, "require", None)]
    if ca_path:
        checks.append(_tls_check("verify_full_with_supplied_ca", gateway, "verify-full", ca_path))
        checks.append(_tls_check("verify_ca_with_supplied_ca", gateway, "verify-ca", ca_path))
    else:
        checks.append({"check": "verify_full_with_supplied_ca", "outcome": "not_run",
                       "note": "PROBE_DB_CA_PEM is not set"})
    checks.append(_tls_check("verify_full_system_roots", gateway, "verify-full", "system"))
    return checks


# ------------------------------------------------------------------------ role bootstrap


@dataclass
class Recorder:
    """Runs statements on one session and records every outcome."""

    session: DbSession
    secrets_to_hide: list[str]
    steps: list[dict[str, Any]]

    def run(self, step: str, sql: str, *, shown: str | None = None, expect: str = "ok",
            note: str | None = None,
            check: Callable[[dict[str, Any]], bool] | None = None) -> bool:
        """Run `sql`, record the outcome. `expect` is "ok", "error" or "any" (informational: any
        outcome is as expected). `check`, if given, must also hold on the record for the step to
        count as expected."""
        record: dict[str, Any] = {"step": step, "statement": shown or sql, "expect": expect}
        try:
            rows = self.session.execute(sql)
            record["outcome"] = "ok"
            if rows and len(rows) <= 3 and len(str(rows)) < 200:
                record["rows"] = [[str(c) for c in row] for row in rows]
        except DbError as exc:
            record.update(outcome="error", sqlstate=exc.sqlstate,
                          error=scrub(exc.message, self.secrets_to_hide))
        matched = expect == "any" or record["outcome"] == expect
        if matched and check is not None:
            matched = bool(check(record))
        record["as_expected"] = matched
        if note:
            record["note"] = note
        self.steps.append(record)
        return record["outcome"] == "ok"

    def skip(self, step: str, reason: str) -> None:
        """A step that could not run because something it depends on failed (INCONCLUSIVE)."""
        self.steps.append({"step": step, "expect": "ok", "outcome": "skipped",
                           "as_expected": False, "note": reason})


def _role_exists(session: DbSession, role: str) -> bool:
    return bool(session.execute(f"SELECT 1 FROM pg_roles WHERE rolname = {quote_literal(role)}"))


def run_bootstrap(admin: DbSession, gateway: DbGateway, passwords: Mapping[str, str],
                  login_sslmode: str = "require") -> list[dict[str, Any]]:
    """Mirror deploy/postgres/init/10-memory-roles.sh as far as a managed cluster allows."""
    hide = list(passwords.values())
    rec = Recorder(admin, hide, [])
    database = admin.execute("SELECT current_database()")[0][0]
    db = quote_ident(database)

    try:
        attrs = admin.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles "
            "WHERE rolname = current_user")[0]
        rec.steps.append({"step": "admin_attributes", "outcome": "ok", "expect": "ok",
                          "as_expected": True,
                          "detail": dict(zip(("superuser", "createrole", "createdb", "canlogin"),
                                             map(bool, attrs)))})
    except (DbError, IndexError) as exc:
        rec.steps.append({"step": "admin_attributes", "outcome": "error", "expect": "ok",
                          "as_expected": False, "error": scrub(getattr(exc, "message", exc), hide)})

    for role in (MIGRATOR, SERVICE):
        ident = quote_ident(role)
        pw = quote_literal(passwords[role])
        if _role_exists(admin, role):
            rec.run(f"alter_role_{role}_rerun",
                    f"ALTER ROLE {ident} WITH LOGIN PASSWORD {pw}",
                    shown=f"ALTER ROLE {ident} WITH LOGIN PASSWORD '***'",
                    note="role already existed (a re-run): password reset instead of CREATE")
        else:
            rec.run(f"create_role_{role}", f"CREATE ROLE {ident} LOGIN PASSWORD {pw}",
                    shown=f"CREATE ROLE {ident} LOGIN PASSWORD '***'")

    schema, migr, svc = quote_ident(SCHEMA), quote_ident(MIGRATOR), quote_ident(SERVICE)
    rec.run("create_schema", f"CREATE SCHEMA IF NOT EXISTS {schema}")
    owner_ok = rec.run("alter_schema_owner", f"ALTER SCHEMA {schema} OWNER TO {migr}")
    rec.run("revoke_public_connect", f"REVOKE ALL ON DATABASE {db} FROM PUBLIC")
    rec.run("grant_connect_service", f"GRANT CONNECT ON DATABASE {db} TO {svc}")
    rec.run("grant_usage_service", f"GRANT USAGE ON SCHEMA {schema} TO {svc}")
    rec.run("default_privileges_for_migrator",
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {migr} IN SCHEMA {schema} "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {svc}")

    def as_role(role: str, label: str, statement: str, *, expect: str = "ok") -> bool:
        set_ok = rec.run(f"set_role_{label}", f"SET ROLE {quote_ident(role)}")
        ran = False
        try:
            if set_ok:
                ran = rec.run(label, statement, expect=expect)
        finally:
            try:
                admin.execute("RESET ROLE")
            except DbError:
                pass
        return ran

    create_tbl = f"CREATE TABLE IF NOT EXISTS {schema}.migrator_table (id integer)"
    table_ok = as_role(MIGRATOR, "create_table_as_migrator", create_tbl)
    if not (owner_ok and table_ok):
        # Fallback evidence: does an explicit membership grant make the owner change and SET ROLE
        # work? (PostgreSQL 16 no longer lets a role creator SET ROLE to the roles it creates.)
        rec.run("fallback_grant_migrator_to_admin", f"GRANT {migr} TO CURRENT_USER")
        rec.run("fallback_grant_service_to_admin", f"GRANT {svc} TO CURRENT_USER")
        if not owner_ok:
            rec.run("alter_schema_owner_after_membership_grant",
                    f"ALTER SCHEMA {schema} OWNER TO {migr}")
        if not table_ok:
            as_role(MIGRATOR, "create_table_as_migrator_after_membership_grant", create_tbl)
    as_role(SERVICE, "service_reads_migrator_table_via_default_privileges",
            f"SELECT count(*) FROM {schema}.migrator_table")
    as_role(SERVICE, "service_cannot_create_table_expected_error",
            f"CREATE TABLE {schema}.should_not_exist (id integer)", expect="error")

    for role in (MIGRATOR, SERVICE):
        step = f"login_as_{role}"
        record: dict[str, Any] = {"step": step, "expect": "ok", "statement": "connect + SELECT current_user"}
        try:
            session = gateway.connect(sslmode=login_sslmode, user=role, password=passwords[role])
            try:
                who = session.execute("SELECT current_user")[0][0]
            finally:
                session.close()
            record["outcome"] = "ok" if who == role else "error"
            record["rows"] = [[str(who)]]
        except DbError as exc:
            record.update(outcome="error", sqlstate=exc.sqlstate, error=scrub(exc.message, hide))
        record["as_expected"] = record["outcome"] == "ok"
        rec.steps.append(record)
    run_migrator_privilege_checks(rec, admin, gateway, passwords, login_sslmode, database)
    return rec.steps


# ----------------------------------------------------- retest R2: migrator privileges


def _login_step(rec: Recorder, gateway: DbGateway, role: str, step: str, password: str,
                sslmode: str) -> DbSession | None:
    """A real login as `role`; records the step and returns the open session (caller closes)."""
    record: dict[str, Any] = {"step": step, "expect": "ok", "statement": "connect + SELECT current_user"}
    session: DbSession | None = None
    try:
        session = gateway.connect(sslmode=sslmode, user=role, password=password)
        who = session.execute("SELECT current_user")[0][0]
        record["outcome"] = "ok" if who == role else "error"
        record["rows"] = [[str(who)]]
    except DbError as exc:
        record.update(outcome="error", sqlstate=exc.sqlstate, error=scrub(exc.message, rec.secrets_to_hide))
        if session is not None:
            session.close()
        session = None
    record["as_expected"] = record["outcome"] == "ok"
    rec.steps.append(record)
    if session is not None and record["outcome"] != "ok":
        session.close()
        return None
    return session


def _permission_denied(record: dict[str, Any]) -> bool:
    return record.get("sqlstate") == "42501"


def run_migrator_privilege_checks(rec: Recorder, admin: DbSession, gateway: DbGateway,
                                  passwords: Mapping[str, str], login_sslmode: str,
                                  database: str) -> None:
    """Retest R2. Appended after the original steps; nothing here alters the main database's owner.

    The previous `login_as_probe_migrator` step failed on 2026-11-12 for lack of CONNECT on a
    database the platform created (owned by the admin, public CONNECT revoked). These steps test
    the repair and the other privileges a migrator needs on such a database.
    """
    db, migr, schema = quote_ident(database), quote_ident(MIGRATOR), quote_ident(SCHEMA)
    migr_lit, db_lit, schema_lit = quote_literal(MIGRATOR), quote_literal(database), quote_literal(SCHEMA)

    # (1) CONNECT for the migrator, then a real login.
    rec.run("grant_connect_migrator", f"GRANT CONNECT ON DATABASE {db} TO {migr}",
            note="repairs the 2026-11-12 failure; repeatable")
    session = _login_step(rec, gateway, MIGRATOR, "login_as_probe_migrator_after_connect_grant",
                          passwords[MIGRATOR], login_sslmode)

    # (2) Database owner change in SCRATCH databases only (admin session; never the main database).
    names = {"a": f"{SCRATCH_DB_PREFIX}a", "b": f"{SCRATCH_DB_PREFIX}b"}
    for variant in ("a", "b"):
        rec.run(f"scratch_database_cleanup_before_{variant}",
                f"DROP DATABASE IF EXISTS {quote_ident(names[variant])}", expect="any",
                note="re-run safety: removes a scratch database a previous run left behind")
    rec.run("reset_set_option_on_migrator", f"REVOKE SET OPTION FOR {migr} FROM CURRENT_USER",
            expect="any",
            note="informational: puts the admin's SET option on the migrator back to its default "
                 "so variant A really runs without a membership grant (PostgreSQL 16 and later)")
    rec.run("membership_state_before_variant_a",
            f"SELECT pg_has_role(current_user, {migr_lit}, 'MEMBER'), "
            f"pg_has_role(current_user, {migr_lit}, 'SET')", expect="any",
            note="informational: rows are [member, can SET ROLE]")
    scratch_a = quote_ident(names["a"])
    if rec.run("create_scratch_database_a", f"CREATE DATABASE {scratch_a}"):
        rec.run("alter_scratch_database_owner_without_membership_grant",
                f"ALTER DATABASE {scratch_a} OWNER TO {migr}",
                note="variant A: as the admin, no GRANT of the migrator role first")
    else:
        rec.skip("alter_scratch_database_owner_without_membership_grant",
                 "scratch database a could not be created")
    scratch_b = quote_ident(names["b"])
    grant_ok = rec.run("grant_migrator_to_admin_with_set_true",
                       f"GRANT {migr} TO CURRENT_USER WITH SET TRUE",
                       note="variant B first step; the design's step 3")
    if not grant_ok:
        grant_ok = rec.run("grant_migrator_to_admin_plain", f"GRANT {migr} TO CURRENT_USER",
                           note="variant B fallback if WITH SET TRUE was refused")
    if rec.run("create_scratch_database_b", f"CREATE DATABASE {scratch_b}"):
        rec.run("alter_scratch_database_owner_after_membership_grant",
                f"ALTER DATABASE {scratch_b} OWNER TO {migr}",
                note="variant B: after the membership grant")
    else:
        rec.skip("alter_scratch_database_owner_after_membership_grant",
                 "scratch database b could not be created")
    rec.run("scratch_database_owners",
            "SELECT datname, pg_get_userbyid(datdba) FROM pg_database WHERE datname IN "
            f"({quote_literal(names['a'])}, {quote_literal(names['b'])}) ORDER BY datname",
            expect="any", note="informational: who owns each scratch database now")
    for variant in ("a", "b"):
        rec.run(f"drop_scratch_database_{variant}", f"DROP DATABASE IF EXISTS {quote_ident(names[variant])}",
                note="cleanup; a failure here means a scratch database is left behind")

    # (3) and (4) as the migrator, on the main database, with the schema already existing.
    rec.run("precondition_migrator_connect_yes_create_no",
            f"SELECT has_database_privilege({migr_lit}, {db_lit}, 'CONNECT'), "
            f"has_database_privilege({migr_lit}, {db_lit}, 'CREATE')",
            check=lambda r: r.get("rows") == [["True", "False"]],
            note="the test below is only meaningful if the migrator can connect and lacks CREATE "
                 "on the database")
    rec.run("precondition_schema_owned_by_migrator",
            f"SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname = {schema_lit}",
            check=lambda r: r.get("rows") == [[MIGRATOR]])
    if session is None:
        for step in ("migrator_create_schema_if_not_exists_existing_schema",
                     "migrator_existence_check_then_skip", "migrator_creates_table_in_existing_schema",
                     "migrator_create_other_schema_expect_error"):
            rec.skip(step, "the migrator could not log in")
        return
    try:
        as_migrator = Recorder(session, rec.secrets_to_hide, rec.steps)
        as_migrator.run("migrator_create_schema_if_not_exists_existing_schema",
                        f"CREATE SCHEMA IF NOT EXISTS {schema}", expect="error",
                        check=lambda r: _permission_denied(r) and "database" in r.get("error", "").lower(),
                        note="expected: permission denied for database although the schema exists; "
                             "this is why the migration runner must not issue the statement")
        as_migrator.run("migrator_existence_check_then_skip",
                        f"SELECT 1 FROM pg_namespace WHERE nspname = {schema_lit}",
                        check=lambda r: r.get("rows") == [["1"]],
                        note="the replacement: look the schema up, skip creating it when present")
        as_migrator.run("migrator_creates_table_in_existing_schema",
                        f"CREATE TABLE IF NOT EXISTS {schema}.migrator_login_table (id integer)",
                        note="after skipping the create, the migrator can still work in its schema")
        as_migrator.run("migrator_create_other_schema_expect_error",
                        f"CREATE SCHEMA {quote_ident(OTHER_SCHEMA)}", expect="error",
                        check=_permission_denied,
                        note="expected: the migrator cannot create any other schema")
        as_migrator.run("drop_other_schema_cleanup",
                        f"DROP SCHEMA IF EXISTS {quote_ident(OTHER_SCHEMA)}", expect="any",
                        note="cleanup, only does anything if the previous step wrongly succeeded")
    finally:
        session.close()


def r2_findings(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Plain facts read off the R2 steps (the runbook turns them into PASS, FAIL, INCONCLUSIVE)."""
    by = {s["step"]: s for s in steps}

    def outcome(name: str) -> str | None:
        return by[name]["outcome"] if name in by else None

    return {
        "original_migrator_login": outcome("login_as_probe_migrator"),
        "migrator_login_after_connect_grant": outcome("login_as_probe_migrator_after_connect_grant"),
        "alter_database_owner_without_membership_grant":
            outcome("alter_scratch_database_owner_without_membership_grant"),
        "alter_database_owner_after_membership_grant":
            outcome("alter_scratch_database_owner_after_membership_grant"),
        "create_schema_if_not_exists_on_existing_schema_as_migrator":
            outcome("migrator_create_schema_if_not_exists_existing_schema"),
        "existence_check": outcome("migrator_existence_check_then_skip"),
        "migrator_create_other_schema": outcome("migrator_create_other_schema_expect_error"),
        "preconditions_hold": all(by.get(n, {}).get("as_expected") is True for n in (
            "precondition_migrator_connect_yes_create_no", "precondition_schema_owned_by_migrator")),
    }


# -------------------------------------------------------------------------------- marker

MARKER_OLD_DDL = ("(run_number integer NOT NULL, written_at timestamptz NOT NULL DEFAULT now(), "
                  "job_mode text NOT NULL, summary text)")
MARKER_ADD_COLUMNS = ("ADD COLUMN IF NOT EXISTS job_tag text, ADD COLUMN IF NOT EXISTS attempt_id text, "
                      "ADD COLUMN IF NOT EXISTS started_at text, ADD COLUMN IF NOT EXISTS platform_ids text")


def write_marker(session: DbSession, summary: dict[str, Any], mode: str,
                 attempt: Mapping[str, Any] | None = None,
                 hide: list[str] | None = None) -> dict[str, Any]:
    """Create the marker table (scratch schema, else public) and insert one timestamped row.

    The table keeps its original columns; the tag, attempt id, start time and platform
    identifiers are nullable columns added with ADD COLUMN IF NOT EXISTS, so a table created by
    the first probe version still works. If the columns cannot be added the row is written in the
    old shape and `extended_columns` is false.
    """
    payload = json.dumps(summary, sort_keys=True)[:4000]
    attempt = attempt or {}
    errors: list[str] = []
    for table in ("probe_scratch.probe_marker", "public.probe_marker"):
        try:
            session.execute(f"CREATE TABLE IF NOT EXISTS {table} {MARKER_OLD_DDL}")
            extended = True
            try:
                session.execute(f"ALTER TABLE {table} {MARKER_ADD_COLUMNS}")
            except DbError as exc:
                extended = False
                errors.append(f"{table}: could not add tag columns: {exc.sqlstate or 'error'}")
            run_number = int(session.execute(f"SELECT count(*) + 1 FROM {table}")[0][0])
            if extended:
                ids = scrub(json.dumps(attempt.get("platform_identifiers", {}), sort_keys=True),
                            hide or [], limit=2000)
                session.execute(
                    f"INSERT INTO {table} (run_number, job_mode, job_tag, attempt_id, started_at, "
                    f"platform_ids, summary) VALUES ({run_number}, {quote_literal(mode)}, "
                    f"{quote_literal(str(attempt.get('job_tag', '(none)')))}, "
                    f"{quote_literal(str(attempt.get('attempt_id', '')))}, "
                    f"{quote_literal(str(attempt.get('started_at', '')))}, {quote_literal(ids)}, "
                    f"{quote_literal(payload)})")
            else:
                session.execute(
                    f"INSERT INTO {table} (run_number, job_mode, summary) "
                    f"VALUES ({run_number}, {quote_literal(mode)}, {quote_literal(payload)})")
            return {"written": True, "table": table, "run_number": run_number,
                    "extended_columns": extended, "errors_before_success": errors}
        except DbError as exc:
            errors.append(f"{table}: {exc.sqlstate or 'error'} {exc.message}")
    return {"written": False, "table": None, "errors": errors}


# ---------------------------------------------------------------------------------- run


def run_job(environ: Mapping[str, str], gateway: DbGateway | None = None,
            attempt: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    """Run everything; return (summary, exit code). `gateway` is injected by tests; `attempt`
    (from `build_attempt`) lets `main` log the attempt before anything else happens."""
    attempt = attempt or build_attempt(environ)
    mode = attempt["job_mode"]
    summary: dict[str, Any] = {"probe": "job", "started_at": attempt["started_at"], "job_mode": mode,
                               "job_tag": attempt["job_tag"], "attempt_id": attempt["attempt_id"],
                               "platform_identifiers": attempt["platform_identifiers"],
                               "variables": variable_report(environ)}
    exit_code = EXIT_FAIL_MODE if mode == "fail" else 0
    info: ConnectionInfo | None = None
    try:
        info = parse_connection_info(environ.get("PROBE_DB_URL") or environ.get("DATABASE_URL"))
        summary["connection_info"] = info.public()
    except ConnectionInfoError as exc:
        summary["connection_info"] = {"error": str(exc)}
    if gateway is None and info is not None:
        gateway = PsycopgGateway(info)
    if gateway is None:
        summary["finished_at"] = utc_now_iso()
        summary["note"] = "no usable database connection information; nothing else was run"
        return summary, exit_code

    ca_pem: str | None = None
    if environ.get("PROBE_DB_CA_PEM"):
        try:
            ca_pem, form = decode_pem_variable(environ.get("PROBE_DB_CA_PEM"), "PROBE_DB_CA_PEM")
            summary["db_ca_variable_form"] = form
        except PemError as exc:
            summary["db_ca_variable_form"] = f"unusable: {exc}"
    try:
        if ca_pem:
            with TempSecretFiles({"db-ca.crt": ca_pem}) as paths:
                summary["tls"] = run_tls_checks(gateway, str(paths["db-ca.crt"]))
        else:
            summary["tls"] = run_tls_checks(gateway, None)
    except Exception as exc:  # noqa: BLE001 - report, keep going
        summary["tls"] = [{"check": "tls_checks", "outcome": "error", "error": scrub(exc)}]

    passwords = {MIGRATOR: secrets.token_urlsafe(24), SERVICE: secrets.token_urlsafe(24)}
    hide = (info.secret_values() if info else []) + list(passwords.values())
    try:
        admin = gateway.connect(sslmode="require")
    except DbError as exc:
        summary["bootstrap"] = [{"step": "connect_as_admin", "outcome": "error",
                                 "sqlstate": exc.sqlstate,
                                 "error": scrub(exc.message, hide)}]
        summary["marker"] = {"written": False, "errors": ["no admin connection"]}
    else:
        try:
            summary["bootstrap"] = run_bootstrap(admin, gateway, passwords)
            steps = summary["bootstrap"]
            summary["bootstrap_summary"] = {
                "steps": len(steps),
                "as_expected": sum(1 for s in steps if s.get("as_expected")),
                "unexpected": [s["step"] for s in steps if not s.get("as_expected")],
                "rerun_detected": any(s["step"].endswith("_rerun") for s in steps),
            }
            summary["r2_findings"] = r2_findings(steps)
            summary["finished_at"] = utc_now_iso()
            summary["marker"] = write_marker(
                admin, {"started_at": summary["started_at"], "finished_at": summary["finished_at"],
                        "bootstrap_summary": summary["bootstrap_summary"]}, mode, attempt, hide)
        except Exception as exc:  # noqa: BLE001
            summary["error"] = scrub(f"{type(exc).__name__}: {exc}", hide)
            exit_code = exit_code or 1
        finally:
            admin.close()
    summary.setdefault("finished_at", utc_now_iso())
    summary["exit_code"] = exit_code
    # Defence in depth: one last pass over everything that will be logged.
    return scrub_tree(summary, hide), exit_code


def main(environ: Mapping[str, str] | None = None, gateway: DbGateway | None = None) -> int:
    env = os.environ if environ is None else environ
    attempt = build_attempt(env)
    # One line before anything else runs, so an attempt is visible in the logs even if the job is
    # killed part way (retest R3: count these lines per tag to count attempts).
    log_json("probe_job_attempt_started", job_tag=attempt["job_tag"], job_mode=attempt["job_mode"],
             attempt_id=attempt["attempt_id"], started_at=attempt["started_at"],
             platform_identifiers=attempt["platform_identifiers"])
    summary, code = run_job(env, gateway, attempt)
    log_json("probe_job_summary", **summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
