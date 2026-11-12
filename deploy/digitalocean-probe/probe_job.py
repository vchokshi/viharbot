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

Environment: PROBE_DB_URL (preferred) or DATABASE_URL, PROBE_DB_CA_PEM (optional), PROBE_JOB_FAIL.
The database is reached through `DbGateway`, so the logic is tested with a fake and no database.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Any, Mapping

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
                      "PROBE_DB_USERNAME", "PROBE_DB_CA_PEM", "PROBE_JOB_FAIL")


def variable_report(environ: Mapping[str, str]) -> dict[str, Any]:
    """Which expected variable names are present, and every database-looking name. Names only."""
    return {
        "expected_present": {name: bool(environ.get(name)) for name in EXPECTED_VARIABLES},
        "database_like_names": [n for n in env_names(environ)
                                if any(h in n.upper() for h in DB_NAME_HINTS)],
    }


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
            note: str | None = None) -> bool:
        record: dict[str, Any] = {"step": step, "statement": shown or sql, "expect": expect}
        try:
            rows = self.session.execute(sql)
            record["outcome"] = "ok"
            if rows and len(rows) <= 3 and len(str(rows)) < 200:
                record["rows"] = [[str(c) for c in row] for row in rows]
        except DbError as exc:
            record.update(outcome="error", sqlstate=exc.sqlstate,
                          error=scrub(exc.message, self.secrets_to_hide))
        record["as_expected"] = record["outcome"] == expect
        if note:
            record["note"] = note
        self.steps.append(record)
        return record["outcome"] == "ok"


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
    return rec.steps


# -------------------------------------------------------------------------------- marker


def write_marker(session: DbSession, summary: dict[str, Any], mode: str) -> dict[str, Any]:
    """Create the marker table (scratch schema, else public) and insert one timestamped row."""
    payload = json.dumps(summary, sort_keys=True)[:4000]
    errors: list[str] = []
    for table in ("probe_scratch.probe_marker", "public.probe_marker"):
        try:
            session.execute(
                f"CREATE TABLE IF NOT EXISTS {table} (run_number integer NOT NULL, "
                "written_at timestamptz NOT NULL DEFAULT now(), job_mode text NOT NULL, "
                "summary text)")
            run_number = int(session.execute(f"SELECT count(*) + 1 FROM {table}")[0][0])
            session.execute(
                f"INSERT INTO {table} (run_number, job_mode, summary) "
                f"VALUES ({run_number}, {quote_literal(mode)}, {quote_literal(payload)})")
            return {"written": True, "table": table, "run_number": run_number,
                    "errors_before_success": errors}
        except DbError as exc:
            errors.append(f"{table}: {exc.sqlstate or 'error'} {exc.message}")
    return {"written": False, "table": None, "errors": errors}


# ---------------------------------------------------------------------------------- run


def run_job(environ: Mapping[str, str], gateway: DbGateway | None = None) -> tuple[dict[str, Any], int]:
    """Run everything; return (summary, exit code). `gateway` is injected by tests."""
    mode = "fail" if (environ.get("PROBE_JOB_FAIL", "").lower() in ("1", "true", "yes")) else "normal"
    summary: dict[str, Any] = {"probe": "job", "started_at": utc_now_iso(), "job_mode": mode,
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
            summary["finished_at"] = utc_now_iso()
            summary["marker"] = write_marker(
                admin, {"started_at": summary["started_at"], "finished_at": summary["finished_at"],
                        "bootstrap_summary": summary["bootstrap_summary"]}, mode)
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
    summary, code = run_job(env, gateway)
    log_json("probe_job_summary", **summary)
    return code


if __name__ == "__main__":
    sys.exit(main())
