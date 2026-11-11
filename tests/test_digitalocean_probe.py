"""Offline tests for the throwaway DigitalOcean probe kit (deploy/digitalocean-probe/).

Loopback only: nothing here contacts DigitalOcean or any real database. Passwords, certificates and
tokens are built at runtime, never written as literals.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import socket
import ssl
import stat
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.x509.oid import ExtensionOID

ROOT = Path(__file__).resolve().parent.parent
PROBE_DIR = ROOT / "deploy" / "digitalocean-probe"
sys.path.insert(0, str(PROBE_DIR))

import gen_probe_cert  # noqa: E402
import probe_client  # noqa: E402
import probe_common  # noqa: E402
import probe_job  # noqa: E402
import probe_outsider  # noqa: E402
import probe_service  # noqa: E402
from probe_common import DbError  # noqa: E402


def rand_secret(label: str = "") -> str:
    """A runtime-built throwaway value that no scanner can mistake for a committed secret."""
    import secrets

    return f"{label}{secrets.token_urlsafe(18)}"


def make_cert(sans: list[str]) -> tuple[str, str]:
    cert, key = gen_probe_cert.generate_certificate(sans)
    return cert.decode(), key.decode()


# --------------------------------------------------------------------------- server helpers


def serve(server) -> tuple[str, int]:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return "127.0.0.1", server.server_address[1]


@pytest.fixture()
def plain_server():
    server = probe_service.make_server("127.0.0.1", 0, None, max_body=2 * 1024 * 1024)
    host, port = serve(server)
    yield host, port
    server.shutdown()
    server.server_close()


@pytest.fixture()
def tls_server():
    cert, key = make_cert(["probe-service", "127.0.0.1"])
    context = probe_service.build_server_context(cert, key)
    server = probe_service.make_server("127.0.0.1", 0, context,
                                       cert_sha256=probe_common.pem_sha256_fingerprint(cert))
    host, port = serve(server)
    yield host, port, cert
    server.shutdown()
    server.server_close()


def raw_exchange(host: str, port: int, payload: bytes) -> tuple[bytes, bytes]:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.sendall(payload)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    return head, body


# ------------------------------------------------------------------------------ echo service


TRICKY_TARGETS = [
    "/echo/plain",
    "//double//slash/./dot/../x",
    "/echo?b=2&a=1&a=0",
    "/echo?a=0&a=1&b=2",
    "/echo/x?",
    "/echo?a&a=&b=&=c",
    "/echo/%E4%2F%7E?q=%E4%7E%2F",
    "/echo/%e4%2f%7e?q=%e4%7e%2f",
    "/echo?q=a+b&r=a%20b",
    "/echo/%E2%9C%93?name=%E6%97%A5",
]


@pytest.mark.parametrize("target", TRICKY_TARGETS)
def test_echo_returns_raw_target_exactly(plain_server, target):
    host, port = plain_server
    head, body = raw_exchange(
        host, port, f"GET {target} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\n\r\n".encode())
    assert head.startswith(b"HTTP/1.0 200") or head.startswith(b"HTTP/1.1 200")
    echoed = json.loads(body)
    assert echoed["method"] == "GET"
    assert echoed["raw_target"] == target
    assert echoed["body_length"] == 0


def test_echo_body_hash_methods_and_chunked_framing(plain_server):
    import hashlib

    host, port = plain_server
    payload = bytes(range(256)) + "日本語".encode()
    request = (f"PUT /echo/p?x=1 HTTP/1.1\r\nHost: h\r\nContent-Type: application/octet-stream\r\n"
               f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n").encode() + payload
    echoed = json.loads(raw_exchange(host, port, request)[1])
    assert echoed["method"] == "PUT"
    assert echoed["body_sha256"] == hashlib.sha256(payload).hexdigest()
    assert echoed["body_framing"] == "content-length"
    assert echoed["safe_headers"]["content-type"] == "application/octet-stream"

    chunked = (b"POST /c HTTP/1.1\r\nHost: h\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
               b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n")
    echoed = json.loads(raw_exchange(host, port, chunked)[1])
    assert echoed["body_framing"] == "chunked"
    assert echoed["body_sha256"] == hashlib.sha256(b"hello world").hexdigest()


def test_echo_reports_header_names_but_not_unlisted_values(plain_server):
    host, port = plain_server
    marker = rand_secret("v")
    request = (f"GET /h HTTP/1.1\r\nHost: {host}\r\nX-Mixed-Case-Name: {marker}\r\n"
               f"Authorization: Bearer {marker}\r\nConnection: close\r\n\r\n").encode()
    body = raw_exchange(host, port, request)[1]
    echoed = json.loads(body)
    assert "X-Mixed-Case-Name" in echoed["header_names"] and "Authorization" in echoed["header_names"]
    assert marker.encode() not in body  # names only: unlisted header values are never echoed
    assert set(echoed["safe_headers"]) <= {"content-type", "content-length", "host", "transfer-encoding"}


def test_reserved_paths_and_body_limit(plain_server):
    host, port = plain_server
    health = json.loads(raw_exchange(host, port, b"GET /__probe/health HTTP/1.0\r\n\r\n")[1])
    assert health == {"ok": True}
    info = json.loads(raw_exchange(host, port, b"GET /__probe/info HTTP/1.0\r\n\r\n")[1])
    assert info["probe"] == "service" and "key" not in json.dumps(info).lower().replace("cert_sha256", "")
    big = b"x" * (3 * 1024 * 1024)
    head, _ = raw_exchange(host, port, (f"POST /b HTTP/1.1\r\nHost: h\r\nContent-Length: {len(big)}\r\n"
                                         "Connection: close\r\n\r\n").encode())
    assert b" 413 " in head.split(b"\r\n")[0]


def test_full_echo_suite_is_exact_over_plain_loopback(plain_server):
    host, port = plain_server
    results = probe_client.run_echo_suite(host, port, None, large_kb=300)
    assert len(results) == len(probe_client.build_cases(300))
    assert [r["case"] for r in results if not r["exact"]] == []
    assert any(r["case"] == "post-large-300kb" and r["body_length"] == 300 * 1024 for r in results)


def test_full_echo_suite_is_exact_over_verified_tls(tls_server):
    host, port, cert = tls_server
    context = probe_client.build_pinned_context(cert)
    results = probe_client.run_echo_suite(host, port, context, large_kb=64)
    assert all(r["exact"] for r in results), [r for r in results if not r["exact"]]


# ------------------------------------------------------------------------ comparison logic


def correct_echo(sent: probe_client.SentRequest) -> dict:
    return probe_service.echo_payload(
        f"{sent.method} {sent.target} HTTP/1.1\r\n".encode(),
        [("Host", "probe-service:8443"), ("Content-Length", str(len(sent.body or b"")))],
        sent.body or b"", "content-length" if sent.body else "none")


def make_sent(**overrides) -> probe_client.SentRequest:
    args = dict(name="t", method="POST", target="/echo/a%2Fb?x=%7E&y=1&y=0&z=%e4",
                body=b"payload-bytes", content_type="application/json")
    args.update(overrides)
    return probe_client.SentRequest(**args)


def test_compare_exact_when_nothing_changed():
    sent = make_sent()
    outcome = probe_client.compare_exchange(sent, correct_echo(sent))
    assert outcome["exact"] and outcome["diffs"] == []


def test_compare_flags_single_changed_byte():
    sent = make_sent()
    received = correct_echo(make_sent(body=b"payload-bytez"))
    outcome = probe_client.compare_exchange(sent, received)
    assert not outcome["exact"]
    assert [d["field"] for d in outcome["diffs"]] == ["body_sha256"]  # same length, one byte


@pytest.mark.parametrize("changed_target", [
    "/echo/a%2Fb?x=~&y=1&y=0&z=%e4",          # %7E re-encoded as ~
    "/echo/a%2Fb?x=%7E&y=0&y=1&z=%e4",        # repeated key reordered
    "/echo/a%2Fb?y=1&y=0&x=%7E&z=%e4",        # different keys reordered
    "/echo/a%2Fb?x=%7E&y=1&y=0&z=%E4",        # hex digit re-cased
    "/echo/a%2fb?x=%7E&y=1&y=0&z=%e4",        # hex digit re-cased in the path
    "/echo/a/b?x=%7E&y=1&y=0&z=%e4",          # %2F decoded
    "/echo/a%2Fb?x=%7E&y=1&y=0&z=%e4&",       # trailing separator added
    "/echo/a%2Fb?x=%7E&y=1&y=0&z=%e4#",       # fragment appended
])
def test_compare_flags_target_changes(changed_target):
    sent = make_sent()
    outcome = probe_client.compare_exchange(sent, correct_echo(make_sent(target=changed_target)))
    assert not outcome["exact"]
    assert [d["field"] for d in outcome["diffs"]] == ["raw_target"]


def test_compare_flags_method_and_length_and_notes_header_changes():
    sent = make_sent()
    received = correct_echo(make_sent(method="PUT", body=b"payload-bytes!"))
    received["safe_headers"]["content-type"] = "text/plain"
    received["header_names"].append("X-Forwarded-For")
    outcome = probe_client.compare_exchange(sent, received)
    assert {d["field"] for d in outcome["diffs"]} == {"method", "body_sha256", "body_length"}
    assert any("content-type changed" in n for n in outcome["notes"])
    assert any("x-forwarded-for" in n for n in outcome["notes"])


# --------------------------------------------------------------- TLS service and pinned client


def test_service_refuses_to_start_without_certificate(capsys):
    with pytest.raises(probe_service.MissingCertificateError):
        probe_service.load_tls_material({})
    key_value = rand_secret("k")
    assert probe_service.main({"PROBE_TLS_KEY_PEM": key_value}) == 2
    err = capsys.readouterr().err
    assert "refusing to start" in err and "PROBE_TLS_CERT_PEM" in err and key_value not in err


def test_service_refuses_mismatched_certificate_and_key_without_leaking(capsys):
    cert, _ = make_cert(["probe-service"])
    _, other_key = make_cert(["probe-service"])
    assert probe_service.main({"PROBE_TLS_CERT_PEM": cert, "PROBE_TLS_KEY_PEM": other_key}) == 2
    err = capsys.readouterr().err
    assert "PRIVATE KEY" not in err and other_key.splitlines()[1] not in err


def test_temp_secret_files_are_0600_and_removed(monkeypatch):
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp

    def tracking(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    monkeypatch.setattr(tempfile, "mkdtemp", tracking)
    with probe_common.TempSecretFiles({"a.pem": "one", "b.key": "two"}) as paths:
        for path in paths.values():
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(created[0].stat().st_mode) == 0o700
    assert not created[0].exists()

    with pytest.raises(RuntimeError):
        with probe_common.TempSecretFiles({"c.pem": "three"}):
            raise RuntimeError("boom")
    assert not created[1].exists()


def test_building_server_context_leaves_no_files_behind(monkeypatch):
    created: list[Path] = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: created.append(Path(real_mkdtemp(*a, **k))) or str(created[-1]))
    cert, key = make_cert(["probe-service"])
    probe_service.build_server_context(cert, key)
    assert created and not any(path.exists() for path in created)


def test_verification_on_succeeds_only_for_pinned_certificate_and_matching_name(tls_server):
    host, port, cert = tls_server
    good = probe_client.check_host(host, port, cert, server_name="probe-service")
    assert good["resolves"] and good["connects"] and good["verified"] is True
    assert good["tls_version"] in ("TLSv1.2", "TLSv1.3")

    wrong_name = probe_client.check_host(host, port, cert, server_name="not-the-name")
    assert wrong_name["connects"] and wrong_name["verified"] is False
    assert wrong_name["reason"].startswith("certificate:")
    assert wrong_name["chain_ok_ignoring_hostname"] is True  # the chain is fine, only the name is wrong

    other_cert, _ = make_cert(["probe-service", "127.0.0.1"])
    different = probe_client.check_host(host, port, other_cert, server_name="probe-service")
    assert different["connects"] and different["verified"] is False
    assert different["chain_ok_ignoring_hostname"] is False  # not the pinned certificate at all


def test_system_roots_are_not_trusted():
    context = probe_client.build_pinned_context(make_cert(["probe-service"])[0])
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.cert_store_stats()["x509"] == 1  # only the pinned certificate, no system roots


def test_check_host_reports_dns_and_tcp_failures(monkeypatch):
    cert, _ = make_cert(["probe-service"])

    def no_dns(*args, **kwargs):
        raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")

    monkeypatch.setattr(socket, "getaddrinfo", no_dns)
    missing = probe_client.check_host("probe-service", 8443, cert)
    assert missing["resolves"] is False and missing["reason"].startswith("dns:")
    monkeypatch.undo()

    with socket.socket() as sock:  # a loopback port with nothing listening
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    refused = probe_client.check_host("127.0.0.1", closed, cert, timeout=2)
    assert refused["resolves"] is True and refused["connects"] is False
    assert refused["reason"].startswith("tcp:")


# ---------------------------------------------------------------------- candidate hostnames


def test_candidates_default_configured_and_environment_hints():
    assert probe_client.build_candidates({}, search_domains=[]) == [
        {"host": "probe-service", "port": 8443, "source": "PROBE_CANDIDATE_HOSTS"}]
    env = {
        "PROBE_CANDIDATE_HOSTS": "probe-service, probe-service:9443,other.internal",
        "APP_URL": "https://example-app.ondigitalocean.app/path",
        "DATABASE_URL": "postgresql://u:p@db.example:25060/x",   # sensitive name: never a hint
        "PROBE_TLS_KEY_PEM": "x",
        "SOME_PRIVATE_URL": "http://svc-internal:8080",
        "UNRELATED": "value",
    }
    candidates = probe_client.build_candidates(env, search_domains=["svc.cluster.local"])
    hosts = [(c["host"], c["port"]) for c in candidates]
    assert hosts[:3] == [("probe-service", 8443), ("probe-service", 9443), ("other.internal", 8443)]
    assert ("probe-service.svc.cluster.local", 8443) in hosts
    assert ("example-app.ondigitalocean.app", 8443) in hosts and ("svc-internal", 8443) in hosts
    assert all("db.example" not in h for h, _ in hosts)
    assert len(hosts) == len(set(hosts))


# ------------------------------------------------------------------ connection info parser


def password_variants() -> list[str]:
    return [rand_secret("pw"), rand_secret("a@b/c:d") + "%?#", "p w" + rand_secret("x")]


def build_url(user: str, password: str, host: str, port: str = "25060", extra: str = "?sslmode=require") -> str:
    return f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/probe_db{extra}"


def test_connection_info_never_emits_password_user_or_full_host():
    host = "private-db-postgresql-nyc3-" + rand_secret("n").lower().replace("_", "") + "-do-user-1-0.d.db.ondigitalocean.com"
    for password in password_variants():
        user = "doadmin"
        info = probe_common.parse_connection_info(build_url(user, password, host))
        public = info.public()
        emitted = json.dumps(public) + str(info) + repr(info)
        for secret in (password, quote(password, safe=""), host):
            assert secret not in emitted
        assert public["host_has_private_prefix"] is True
        assert public["port"] == 25060 and public["database"] == "probe_db"
        assert public["sslmode_in_url"] == "require"
        assert public["password_present"] is True and "user" not in public
        assert public["host_redacted"] == "***.db.ondigitalocean.com"


def test_connection_info_errors_never_contain_the_url_or_password():
    password = rand_secret("pw")
    bad = [
        build_url("u", password, "host.example", port="notaport"),
        f"mysql://u:{password}@host.example:3306/x",
        f"postgresql://u:{password}@:25060/x",
        f"postgresql://u:{password}@[::1/x",
        "",
    ]
    for url in bad:
        with pytest.raises(probe_common.ConnectionInfoError) as caught:
            probe_common.parse_connection_info(url)
        assert password not in str(caught.value) and "host.example" not in str(caught.value)
        assert password not in repr(caught.value.__cause__)


def test_connection_info_absent_sslmode_and_ip_host_shapes():
    info = probe_common.parse_connection_info("postgres://a:b@10.1.2.3:5432/db")
    assert info.public()["sslmode_in_url"] == "(absent)"
    assert info.public()["host_redacted"] == "10.*.*.*"


def test_scrub_removes_values_urls_and_password_pairs():
    password = rand_secret("pw")
    text = (f"connection to server at db.example failed: password={password} "
            f"url postgresql://doadmin:{password}@db.example:25060/x and again {password}")
    cleaned = probe_common.scrub(text, [password, "db.example"])
    assert password not in cleaned and "db.example" not in cleaned and "postgresql://" not in cleaned


# ------------------------------------------------------------------------- fake database


class FakeDb:
    """In-memory stand-in for a cluster: roles, scratch schema, marker table, scripted failures."""

    def __init__(self, *, fail: dict[str, DbError] | None = None, membership_needed: bool = False,
                 fail_modes: set[str] | None = None) -> None:
        self.roles: dict[str, str | None] = {}
        self.marker: list[tuple[int, str, str]] = []
        self.log: list[str] = []
        self.connects: list[tuple[str, str | None]] = []
        self.fail = fail or {}
        self.fail_modes = fail_modes or set()
        self.membership_needed = membership_needed
        self.members: set[str] = set()

    def connect(self, *, sslmode, sslrootcert=None, user=None, password=None):
        self.connects.append((sslmode, sslrootcert))
        if sslmode in self.fail_modes:
            raise DbError("08006", "connection failed: certificate verify failed")
        if user is not None and self.roles.get(user) != password:
            raise DbError("28P01", f"password authentication failed for user {user}")
        return FakeSession(self, user or "doadmin")


class FakeSession:
    def __init__(self, db: FakeDb, user: str) -> None:
        self.db, self.login, self.current = db, user, user

    def close(self) -> None:
        pass

    def execute(self, sql: str):
        db = self.db
        db.log.append(sql)
        for needle, error in db.fail.items():
            if needle in sql:
                raise error
        if sql.startswith("SHOW server_version"):
            return [("17.2",)]
        if "pg_stat_ssl" in sql:
            return [(True, "TLSv1.3")]
        if sql.startswith("SELECT current_database"):
            return [("probe",)]
        if "FROM pg_roles WHERE rolname = current_user" in sql:
            return [(False, True, False, True)]
        if sql.startswith("SELECT 1 FROM pg_roles"):
            name = sql.split("'")[1]
            return [(1,)] if name in db.roles else []
        if sql.startswith("SELECT current_user"):
            return [(self.current,)]
        if sql.startswith("CREATE ROLE") or sql.startswith("ALTER ROLE"):
            name = sql.split('"')[1]
            db.roles[name] = sql.split("PASSWORD '")[1].rstrip("'")
            return []
        if sql.startswith("GRANT") and "TO CURRENT_USER" in sql:
            db.members.add(sql.split('"')[1])
            return []
        if sql.startswith("ALTER SCHEMA") and db.membership_needed and "probe_migrator" not in db.members:
            raise DbError("42501", "must be able to SET ROLE \"probe_migrator\"")
        if sql.startswith("SET ROLE"):
            role = sql.split('"')[1]
            if db.membership_needed and role not in db.members:
                raise DbError("42501", "permission denied to set role \"%s\"" % role)
            self.current = role
            return []
        if sql.startswith("RESET ROLE"):
            self.current = self.login
            return []
        if "should_not_exist" in sql and self.current == "probe_service":
            raise DbError("42501", "permission denied for schema probe_scratch")
        if "SELECT count(*) + 1 FROM" in sql:
            return [(len(db.marker) + 1,)]
        if sql.startswith("INSERT INTO"):
            values = sql.split("VALUES (")[1]
            run_number, mode = int(values.split(",")[0]), values.split("'")[1]
            db.marker.append((run_number, mode, values))
            return []
        if "max(run_number)" in sql:
            if not db.marker:
                raise DbError("42P01", 'relation "probe_marker" does not exist')
            return [(len(db.marker), db.marker[-1][0], "2026-11-11 10:00:00+00")]
        if sql.startswith("SELECT job_mode"):
            return [(db.marker[-1][1],)]
        return []


def job_env(*, password: str | None = None, extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {"PROBE_DB_URL": build_url("doadmin", password or rand_secret("pw"),
                                     "db-host-1.d.db.ondigitalocean.com")}
    env.update(extra or {})
    return env


def test_job_normal_run_reports_everything_exits_zero_and_leaks_nothing():
    db = FakeDb()
    password = rand_secret("pw")
    ca_cert, _ = make_cert(["db"])
    env = job_env(password=password, extra={"PROBE_DB_CA_PEM": ca_cert, "PROBE_DB_HOSTNAME": "x"})
    summary, code = probe_job.run_job(env, db)
    assert code == 0
    text = json.dumps(summary)
    created = [sql for sql in db.log if sql.startswith("CREATE ROLE")]
    assert len(created) == 2 and all("LOGIN PASSWORD" in sql for sql in created)
    for sql in created:
        role_password = sql.split("PASSWORD '")[1].rstrip("'")
        assert role_password not in text            # generated role passwords never reported
    assert password not in text and "db-host-1" not in text
    assert summary["connection_info"]["host_redacted"] == "***.db.ondigitalocean.com"
    assert summary["variables"]["expected_present"]["PROBE_DB_URL"] is True
    assert summary["variables"]["expected_present"]["DATABASE_URL"] is False
    checks = {c["check"]: c for c in summary["tls"]}
    assert checks["sslmode_require"]["outcome"] == "ok" and checks["sslmode_require"]["server_version"] == "17.2"
    assert checks["verify_full_with_supplied_ca"]["outcome"] == "ok"
    assert summary["bootstrap_summary"]["unexpected"] == []
    assert summary["marker"] == {"written": True, "table": "probe_scratch.probe_marker",
                                 "run_number": 1, "errors_before_success": []}
    steps = {s["step"]: s for s in summary["bootstrap"]}
    assert steps["service_cannot_create_table_expected_error"]["outcome"] == "error"
    assert steps["service_cannot_create_table_expected_error"]["as_expected"] is True
    assert steps["login_as_probe_service"]["outcome"] == "ok"
    assert not any("PASSWORD 'x" in s.get("statement", "") for s in summary["bootstrap"])
    assert all("'***'" in s["statement"] for s in summary["bootstrap"] if "PASSWORD" in s.get("statement", ""))


def test_job_records_failed_create_role_with_sqlstate_and_scrubbed_text_and_still_exits_zero():
    password = rand_secret("pw")
    error = DbError("42501", f"permission denied to create role (user doadmin, password={password})")
    db = FakeDb(fail={"CREATE ROLE": error})
    summary, code = probe_job.run_job(job_env(password=password), db)
    assert code == 0  # a failed probe step is data, not a job failure
    failed = [s for s in summary["bootstrap"] if s["step"].startswith("create_role_")]
    assert len(failed) == 2 and all(s["outcome"] == "error" and s["sqlstate"] == "42501" for s in failed)
    assert all(password not in s["error"] and "doadmin" not in s["error"] for s in failed)
    assert summary["bootstrap_summary"]["unexpected"]
    assert summary["marker"]["written"] is True
    assert password not in json.dumps(summary)


def test_job_connection_errors_are_scrubbed():
    password = rand_secret("pw")
    db = FakeDb(fail_modes={"require"})
    env = job_env(password=password)
    db_error_gateway = db

    class Leaky:
        def connect(self, **kwargs):
            raise DbError("08006", f"could not connect to db-host-1.d.db.ondigitalocean.com as doadmin "
                                   f"password={password} postgresql://doadmin:{password}@db-host-1/x")

    summary, code = probe_job.run_job(env, Leaky())
    text = json.dumps(summary)
    assert code == 0 and password not in text and "db-host-1" not in text and "postgresql://" not in text
    assert db_error_gateway.log == []


def test_job_rerun_is_idempotent_and_marker_counts_runs():
    db = FakeDb()
    env = job_env()
    first, _ = probe_job.run_job(env, db)
    second, code = probe_job.run_job(env, db)
    assert code == 0
    assert first["bootstrap_summary"]["rerun_detected"] is False
    assert second["bootstrap_summary"]["rerun_detected"] is True
    assert second["bootstrap_summary"]["unexpected"] == []
    assert any(s["step"].startswith("alter_role_") for s in second["bootstrap"])
    assert second["marker"]["run_number"] == 2 and len(db.marker) == 2


def test_job_fail_variant_writes_marker_then_exits_nonzero():
    db = FakeDb()
    summary, code = probe_job.run_job(job_env(extra={"PROBE_JOB_FAIL": "true"}), db)
    assert code == probe_job.EXIT_FAIL_MODE != 0
    assert summary["job_mode"] == "fail" and db.marker[-1][1] == "fail"
    assert probe_job.main(job_env(extra={"PROBE_JOB_FAIL": "true"}), FakeDb()) == probe_job.EXIT_FAIL_MODE
    assert probe_job.main(job_env(), FakeDb()) == 0


def test_job_without_database_information_exits_zero_with_a_note():
    summary, code = probe_job.run_job({"PROBE_JOB_FAIL": "false"}, None)
    assert code == 0 and "error" in summary["connection_info"] and "nothing else was run" in summary["note"]
    assert summary["variables"]["expected_present"]["PROBE_DB_URL"] is False


def test_job_membership_fallback_is_recorded_when_set_role_needs_a_grant():
    db = FakeDb(membership_needed=True)
    summary, _ = probe_job.run_job(job_env(), db)
    steps = {s["step"]: s for s in summary["bootstrap"]}
    assert steps["alter_schema_owner"]["outcome"] == "error" and steps["alter_schema_owner"]["sqlstate"] == "42501"
    assert steps["fallback_grant_migrator_to_admin"]["outcome"] == "ok"
    assert steps["alter_schema_owner_after_membership_grant"]["outcome"] == "ok"
    assert steps["create_table_as_migrator_after_membership_grant"]["outcome"] == "ok"


def test_job_verify_full_failure_is_reported_not_raised():
    ca_cert, _ = make_cert(["db"])
    db = FakeDb(fail_modes={"verify-full"})
    summary, code = probe_job.run_job(job_env(extra={"PROBE_DB_CA_PEM": ca_cert}), db)
    checks = {c["check"]: c for c in summary["tls"]}
    assert code == 0 and checks["verify_full_with_supplied_ca"]["outcome"] == "error"
    assert checks["sslmode_require"]["outcome"] == "ok"


def test_job_without_ca_reports_verify_full_as_not_run():
    summary, _ = probe_job.run_job(job_env(), FakeDb())
    checks = {c["check"]: c for c in summary["tls"]}
    assert checks["verify_full_with_supplied_ca"]["outcome"] == "not_run"


# ------------------------------------------------------------------- ordering observer


def test_ordering_observer_sees_marker_only_after_the_job_and_observes_once():
    db = FakeDb()
    observer = probe_client.OrderingObserver(db)
    observer.observe("first_health_check")
    assert observer.observations["first_health_check"]["marker"]["found"] is False
    probe_job.run_job(job_env(), db)
    observer.observe("first_health_check")  # second call for the same label changes nothing
    assert observer.observations["first_health_check"]["marker"]["found"] is False
    observer.observe("first_request")
    marker = observer.observations["first_request"]["marker"]
    assert marker["found"] is True and marker["rows"] == 1 and marker["latest_job_mode"] == "normal"


def test_ordering_observer_without_database_and_with_errors():
    none_observer = probe_client.OrderingObserver(None)
    none_observer.observe("first_request")
    assert none_observer.observations["first_request"]["marker"]["found"] is None

    class Broken:
        def connect(self, **kwargs):
            raise DbError("28P01", "password authentication failed")

    broken = probe_client.OrderingObserver(Broken())
    broken.observe("first_request")
    assert broken.observations["first_request"]["marker"]["sqlstate"] == "28P01"


# ------------------------------------------------------------------------- client web side


def test_client_results_endpoint_is_token_protected_read_only_and_records_first_request():
    import http.client
    from http.server import ThreadingHTTPServer

    token = rand_secret("t")
    state = probe_client.ProbeState()
    state.results.update(status="done", summary={"echo_all_exact": True})
    observer = probe_client.OrderingObserver(None)
    server = ThreadingHTTPServer(("127.0.0.1", 0), probe_client.make_handler(state, observer, token))
    host, port = serve(server)

    def call(method: str, path: str, headers: dict | None = None):
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request(method, path, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, data

    try:
        assert call("GET", "/health")[0] == 200
        assert "first_health_check" in observer.observations and "first_request" not in observer.observations
        assert call("GET", "/results")[0] == 401
        assert "first_request" in observer.observations
        status, data = call("GET", "/results", {"Authorization": f"Bearer {token}"})
        assert status == 200 and json.loads(data)["summary"]["echo_all_exact"] is True
        assert token.encode() not in data
        assert call("POST", "/results", {"Authorization": f"Bearer {token}"})[0] == 405
        assert call("GET", "/nope")[0] == 404
    finally:
        server.shutdown()
        server.server_close()

    no_token = ThreadingHTTPServer(("127.0.0.1", 0), probe_client.make_handler(state, observer, None))
    host, port = serve(no_token)
    try:
        assert call("GET", "/results")[0] == 503  # never served open
    finally:
        no_token.shutdown()
        no_token.server_close()


def test_run_probe_end_to_end_on_loopback(tls_server):
    host, port, cert = tls_server
    env = {"PROBE_CANDIDATE_HOSTS": f"{host}:{port},no-such-host.invalid:{port}",
           "PROBE_TLS_CA_PEM": base64.b64encode(cert.encode()).decode(), "PROBE_LARGE_BODY_KB": "32",
           "PROBE_STARTUP_WAIT_SECONDS": "1", "DATABASE_URL": build_url("u", rand_secret(), "h.example")}
    state = probe_client.ProbeState()
    results = probe_client.run_probe(env, state, wait_seconds=1)
    assert results["status"] == "done"
    assert results["summary"]["echo_all_exact"] is True
    assert results["summary"]["echo_transport"] == "verified-tls"
    assert results["pinned_certificate"]["variable_form"] == "base64"
    verified = [h for h in results["hosts"] if h["verified"]]
    assert len(verified) == 1 and verified[0]["host"] == host
    assert "DATABASE_URL" in results["environment_variable_names"]
    assert env["DATABASE_URL"] not in json.dumps(results)


def test_run_probe_reports_missing_ca_without_running():
    results = probe_client.run_probe({}, probe_client.ProbeState(), wait_seconds=0)
    assert results["status"] == "error" and "PROBE_TLS_CA_PEM" in results["error"]


# ------------------------------------------------------------------------------- outsider


def test_outsider_reports_reachable_and_unreachable_targets(tls_server):
    host, port, _ = tls_server
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    reached = probe_outsider.run_outsider({"PROBE_TARGET_HOSTS": f"{host}:{port}"})
    assert reached["summary"]["targets_answering_as_probe_service"] == [f"{host}:{port}"]
    assert reached["summary"]["isolated"] is False
    isolated = probe_outsider.run_outsider({"PROBE_TARGET_HOSTS": f"{host}:{closed},bad:port:x",
                                            "PROBE_PUBLIC_URLS": f"http://127.0.0.1:{closed}"})
    assert isolated["summary"]["isolated"] is True
    assert isolated["summary"]["targets_connecting"] == []
    assert isolated["public_urls"][0]["paths"][0].get("error")


# ------------------------------------------------------------------- PEM variable decoding


def test_pem_variable_forms_and_error_messages_do_not_echo_values():
    cert, _ = make_cert(["probe-service"])
    assert probe_common.decode_pem_variable(cert, "X") == (cert, "raw")
    escaped = cert.strip().replace("\n", "\\n")
    assert probe_common.decode_pem_variable(escaped, "X")[0] == cert
    flattened = cert.strip().replace("\n", " ")
    pem, form = probe_common.decode_pem_variable(flattened, "X")
    assert pem == cert and form == "reflowed"
    assert probe_common.decode_pem_variable(base64.b64encode(cert.encode()).decode(), "X") == (cert, "base64")
    junk = rand_secret("junk")
    for bad in (junk, "", None, base64.b64encode(junk.encode()).decode()):
        with pytest.raises(probe_common.PemError) as caught:
            probe_common.decode_pem_variable(bad, "PROBE_X")
        assert junk not in str(caught.value) and "PROBE_X" in str(caught.value)


# ----------------------------------------------------------------------------- generator


def test_generator_sans_validity_and_extensions():
    sans = gen_probe_cert.parse_sans(["probe-service,probe-service.internal", "10.1.2.3", "probe-service"])
    assert sans == ["probe-service", "probe-service.internal", "10.1.2.3"]
    cert_pem, key_pem = gen_probe_cert.generate_certificate(sans)
    cert = x509.load_pem_x509_certificate(cert_pem)
    ext = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME).value
    assert ext.get_values_for_type(x509.DNSName) == ["probe-service", "probe-service.internal"]
    assert [str(ip) for ip in ext.get_values_for_type(x509.IPAddress)] == ["10.1.2.3"]
    lifetime = cert.not_valid_after_utc - cert.not_valid_before_utc
    assert datetime.timedelta(days=364) < lifetime < datetime.timedelta(days=366)
    remaining = cert.not_valid_after_utc - datetime.datetime.now(datetime.timezone.utc)
    assert datetime.timedelta(days=363) < remaining < datetime.timedelta(days=366)
    assert b"PRIVATE KEY" in key_pem
    assert cert.issuer == cert.subject  # self-signed


def test_generator_rejects_bad_sans_and_writes_private_files(tmp_path, capsys):
    with pytest.raises(ValueError):
        gen_probe_cert.parse_sans([" , "])
    with pytest.raises(ValueError):
        gen_probe_cert.generate_certificate(["bad name/with:chars"])
    out = tmp_path / "probe-cert"
    assert gen_probe_cert.main(["--san", "probe-service", "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    key_text = (out / "probe.key.pem").read_text()
    assert "PRIVATE KEY" not in printed
    assert key_text.splitlines()[1] not in printed
    assert base64.b64encode(key_text.encode()).decode() not in printed
    for path in out.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert base64.b64decode((out / "probe.crt.b64").read_text()).decode() == (out / "probe.crt.pem").read_text()
    # the files work end to end with the probe's own loaders
    cert, form = probe_common.decode_pem_variable((out / "probe.crt.b64").read_text(), "X")
    assert form == "base64"
    probe_service.build_server_context(cert, probe_common.decode_pem_variable(
        (out / "probe.key.b64").read_text(), "K")[0])
    assert gen_probe_cert.main(["--out", str(out)]) == 2  # refuses to overwrite
    assert gen_probe_cert.main(["--out", str(out), "--force"]) == 0


def test_generator_refuses_output_inside_repository_outside_data(tmp_path):
    assert gen_probe_cert.check_output_dir(ROOT / "data" / "probe-cert") == (ROOT / "data" / "probe-cert").resolve()
    assert gen_probe_cert.check_output_dir(tmp_path)  # outside the repository
    for bad in (ROOT / "deploy" / "digitalocean-probe", ROOT, ROOT / "docs"):
        with pytest.raises(ValueError):
            gen_probe_cert.check_output_dir(bad)
    assert gen_probe_cert.main(["--out", str(ROOT / "deploy" / "digitalocean-probe" / "certs")]) == 2


def test_data_directory_is_gitignored():
    assert "\ndata/\n" in "\n" + (ROOT / ".gitignore").read_text()


# --------------------------------------------------------------------------- static checks


def test_kit_files_exist_and_specs_are_safe():
    names = {"Dockerfile.probe", "probe_service.py", "probe_client.py", "probe_job.py",
             "probe_outsider.py", "gen_probe_cert.py", "probe-app.yaml", "probe-outsider-app.yaml",
             "RUNBOOK.md"}
    assert names <= {p.name for p in PROBE_DIR.iterdir()}
    for name in ("probe-app.yaml", "probe-outsider-app.yaml"):
        text = (PROBE_DIR / name).read_text()
        assert "BEGIN" not in text and "PRIVATE KEY" not in text
        assert "deploy_on_push: false" in text and "deploy_on_push: true" not in text
        assert "apps-s-1vcpu-0.5gb" in text
        lines = text.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == "type: SECRET":
                assert lines[i - 2].strip() == 'value: ""', f"{name}:{i + 1} secret must have no value"
    app = (PROBE_DIR / "probe-app.yaml").read_text()
    service_block = app.split("- name: probe-service", 1)[1].split("- name: probe-client", 1)[0]
    assert "http_port" not in service_block and "routes" not in service_block
    assert "internal_ports" in service_block
    outsider = (PROBE_DIR / "probe-outsider-app.yaml").read_text()
    assert "http_port" not in outsider and "ingress" not in outsider and "workers:" in outsider


def test_dockerfile_pins_the_repository_psycopg_version_and_runs_non_root():
    docker = (PROBE_DIR / "Dockerfile.probe").read_text()
    pin = "psycopg[binary]==3.3.8"
    assert pin in (ROOT / "pyproject.toml").read_text() and pin in docker
    assert "USER 1001:1001" in docker
    assert "gen_probe_cert" not in docker.replace("gen_probe_cert.py is deliberately not copied", "")
    assert "ENV" not in "".join(line for line in docker.splitlines() if "PASSWORD" in line.upper())


def test_probe_sources_make_no_calls_to_digitalocean_or_third_parties():
    forbidden = ("api.digitalocean.com", "doctl", "urllib.request", "import requests", "telemetry", "sentry")
    for path in PROBE_DIR.glob("*.py"):
        text = path.read_text()
        for word in forbidden:
            assert word not in text, f"{path.name} mentions {word}"
    assert "os.environ" in (PROBE_DIR / "probe_entry.py").read_text()
    assert os.access(PROBE_DIR / "probe_entry.py", os.R_OK)
