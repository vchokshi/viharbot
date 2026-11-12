"""Offline tests for the throwaway DigitalOcean probe kit (deploy/digitalocean-probe/).

Loopback only: nothing here contacts DigitalOcean or any real database. Passwords, certificates and
tokens are built at runtime, never written as literals.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
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
    """In-memory stand-in for a cluster: roles, scratch schema, marker table, scripted failures.

    Options reproduce what the cluster might do: `connect_required` makes the migrator need an
    explicit CONNECT grant (the 2026-11-12 failure); `alter_db_needs_membership` makes ALTER
    DATABASE OWNER need a membership grant; `migrator_has_create` gives the migrator CREATE on the
    database; `legacy_marker` starts with the old marker table (no tag columns); `alter_denied`
    refuses ALTER TABLE on it.
    """

    def __init__(self, *, fail: dict[str, DbError] | None = None, membership_needed: bool = False,
                 fail_modes: set[str] | None = None, connect_required: bool = False,
                 alter_db_needs_membership: bool = False, migrator_has_create: bool = False,
                 legacy_marker: bool = False, alter_denied: bool = False) -> None:
        self.roles: dict[str, str | None] = {}
        # (run_number, mode, raw values, tag, attempt_id, started_at, platform_ids)
        self.marker: list[tuple] = []
        self.log: list[str] = []
        self.connects: list[tuple[str, str | None]] = []
        self.fail = fail or {}
        self.fail_modes = fail_modes or set()
        self.membership_needed = membership_needed
        self.members: set[str] = set()
        self.connect_required = connect_required
        self.connect_granted: set[str] = set()
        self.alter_db_needs_membership = alter_db_needs_membership
        self.migrator_has_create = migrator_has_create
        self.alter_denied = alter_denied
        self.columns_added = not legacy_marker
        self.databases: dict[str, str] = {"probe": "doadmin"}
        self.schema_owner = "doadmin"
        self.other_schemas: set[str] = set()

    def connect(self, *, sslmode, sslrootcert=None, user=None, password=None):
        self.connects.append((sslmode, sslrootcert))
        if sslmode in self.fail_modes:
            raise DbError("08006", "connection failed: certificate verify failed")
        if user is not None and self.roles.get(user) != password:
            raise DbError("28P01", f"password authentication failed for user {user}")
        if user == "probe_migrator" and self.connect_required and user not in self.connect_granted:
            raise DbError("42501", 'permission denied for database "probe"')
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
        if sql.startswith("GRANT CONNECT"):
            db.connect_granted.add(sql.split(" TO ")[1].strip('"'))
            return []
        if sql.startswith("GRANT") and "TO CURRENT_USER" in sql:
            db.members.add(sql.split('"')[1])
            return []
        if sql.startswith("REVOKE SET OPTION"):
            db.members.discard(sql.split('"')[1])
            return []
        if sql.startswith("SELECT pg_has_role"):
            return [("probe_migrator" in db.members, "probe_migrator" in db.members)]
        if sql.startswith("SELECT has_database_privilege"):
            return [(True, db.migrator_has_create)]
        if sql.startswith("SELECT pg_get_userbyid(nspowner)"):
            return [(db.schema_owner,)]
        if sql.startswith("SELECT 1 FROM pg_namespace"):
            return [(1,)]
        if sql.startswith("SELECT datname, pg_get_userbyid(datdba)"):
            return sorted((n, o) for n, o in db.databases.items() if n.startswith("probe_scratch_db_"))
        if sql.startswith("CREATE DATABASE"):
            name = sql.split('"')[1]
            if name in db.databases:
                raise DbError("42P04", f'database "{name}" already exists')
            db.databases[name] = self.login
            return []
        if sql.startswith("DROP DATABASE"):
            db.databases.pop(sql.split('"')[1], None)
            return []
        if sql.startswith("ALTER DATABASE"):
            name = sql.split('"')[1]
            if name == "probe":
                raise AssertionError("the main database's owner must never be altered")
            if db.alter_db_needs_membership and "probe_migrator" not in db.members:
                raise DbError("42501", "must be able to SET ROLE \"probe_migrator\"")
            db.databases[name] = "probe_migrator"
            return []
        if sql.startswith("CREATE SCHEMA") and self.current == "probe_migrator":
            if not db.migrator_has_create:
                raise DbError("42501", 'permission denied for database "probe"')
            if "IF NOT EXISTS" not in sql:
                db.other_schemas.add(sql.split('"')[1])
            return []
        if sql.startswith("DROP SCHEMA"):
            db.other_schemas.discard(sql.split('"')[1])
            return []
        if sql.startswith("ALTER SCHEMA") and db.membership_needed and "probe_migrator" not in db.members:
            raise DbError("42501", "must be able to SET ROLE \"probe_migrator\"")
        if sql.startswith("ALTER SCHEMA"):
            db.schema_owner = "probe_migrator"
            return []
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
        if sql.startswith("ALTER TABLE") and "probe_marker" in sql:
            if db.alter_denied:
                raise DbError("42501", "must be owner of table probe_marker")
            db.columns_added = True
            return []
        if "SELECT count(*) + 1 FROM" in sql:
            return [(len(db.marker) + 1,)]
        if sql.startswith("INSERT INTO"):
            values = sql.split("VALUES (")[1]
            run_number, mode = int(values.split(",")[0]), values.split("'")[1]
            if "job_tag" in sql.split("VALUES")[0]:
                if not db.columns_added:
                    raise DbError("42703", 'column "job_tag" does not exist')
                quoted = re.findall(r"'((?:[^']|'')*)'", values)
                db.marker.append((run_number, mode, values, quoted[1], quoted[2], quoted[3], quoted[4]))
            else:
                db.marker.append((run_number, mode, values, None, None, None, None))
            return []
        if sql.startswith("SELECT coalesce(job_tag") and "GROUP BY" in sql:
            if not db.columns_added:
                raise DbError("42703", 'column "job_tag" does not exist')
            counts: dict[tuple[str, str], int] = {}
            for row in db.marker:
                key = (row[3] or "(none)", row[1])
                counts[key] = counts.get(key, 0) + 1
            return [(t, m, c) for (t, m), c in sorted(counts.items())]
        if sql.startswith("SELECT coalesce(job_tag"):
            if not db.columns_added:
                raise DbError("42703", 'column "job_tag" does not exist')
            return [(r[3] or "(none)", r[1], r[5] or "2026-11-11 10:00:00+00", r[4] or "", r[0], r[6] or "")
                    for r in db.marker]
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
                                 "run_number": 1, "extended_columns": True,
                                 "errors_before_success": []}
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


# ================================================================ retest (2026-11-12 follow-up)

import probe_laptop_dbcheck  # noqa: E402
import time  # noqa: E402

R2_NEW_STEPS = [
    "grant_connect_migrator", "login_as_probe_migrator_after_connect_grant",
    "scratch_database_cleanup_before_a", "scratch_database_cleanup_before_b",
    "reset_set_option_on_migrator", "membership_state_before_variant_a",
    "create_scratch_database_a", "alter_scratch_database_owner_without_membership_grant",
    "grant_migrator_to_admin_with_set_true", "create_scratch_database_b",
    "alter_scratch_database_owner_after_membership_grant", "scratch_database_owners",
    "drop_scratch_database_a", "drop_scratch_database_b",
    "precondition_migrator_connect_yes_create_no", "precondition_schema_owned_by_migrator",
    "migrator_create_schema_if_not_exists_existing_schema", "migrator_existence_check_then_skip",
    "migrator_creates_table_in_existing_schema", "migrator_create_other_schema_expect_error",
    "drop_other_schema_cleanup",
]


def steps_of(summary: dict) -> dict:
    return {s["step"]: s for s in summary["bootstrap"]}


# --------------------------------------------------------------- R2: migrator privileges


def test_r2_steps_run_in_order_after_the_original_steps_which_are_unchanged():
    db = FakeDb()
    summary, code = probe_job.run_job(job_env(), db)
    names = [s["step"] for s in summary["bootstrap"]]
    assert code == 0 and summary["bootstrap_summary"]["unexpected"] == []
    original_login = names.index("login_as_probe_migrator")
    assert names.index("login_as_probe_service") > original_login
    positions = [names.index(step) for step in R2_NEW_STEPS]
    assert positions == sorted(positions) and positions[0] > names.index("login_as_probe_service")
    for step in R2_NEW_STEPS:
        record = steps_of(summary)[step]
        assert {"step", "expect", "outcome", "as_expected"} <= set(record)
        assert record["outcome"] != "error" or record["sqlstate"]
    assert summary["r2_findings"]["preconditions_hold"] is True


def test_r2_reproduces_the_2026_failure_and_shows_the_repair_beside_it():
    db = FakeDb(connect_required=True)
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    assert steps["login_as_probe_migrator"]["outcome"] == "error"
    assert steps["login_as_probe_migrator"]["sqlstate"] == "42501"
    assert steps["login_as_probe_migrator"]["as_expected"] is False
    assert steps["grant_connect_migrator"]["outcome"] == "ok"
    assert steps["login_as_probe_migrator_after_connect_grant"]["outcome"] == "ok"
    assert steps["login_as_probe_migrator_after_connect_grant"]["as_expected"] is True
    names = [s["step"] for s in summary["bootstrap"]]
    assert names.index("login_as_probe_migrator") < names.index("grant_connect_migrator")
    findings = summary["r2_findings"]
    assert findings["original_migrator_login"] == "error"
    assert findings["migrator_login_after_connect_grant"] == "ok"


def test_r2_alter_database_owner_is_tested_in_scratch_databases_only_both_variants():
    db = FakeDb(alter_db_needs_membership=True)
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    a = steps["alter_scratch_database_owner_without_membership_grant"]
    b = steps["alter_scratch_database_owner_after_membership_grant"]
    assert a["outcome"] == "error" and a["sqlstate"] == "42501"
    assert steps["grant_migrator_to_admin_with_set_true"]["outcome"] == "ok"
    assert "WITH SET TRUE" in steps["grant_migrator_to_admin_with_set_true"]["statement"]
    assert b["outcome"] == "ok"
    alters = [sql for sql in db.log if sql.startswith("ALTER DATABASE")]
    assert alters and all("probe_scratch_db_" in sql for sql in alters)
    assert not any(sql.startswith('ALTER DATABASE "probe"') for sql in db.log)
    assert db.databases == {"probe": "doadmin"}  # scratch databases are dropped again
    owners = steps["scratch_database_owners"]["rows"]
    assert [row[0] for row in owners] == ["probe_scratch_db_a", "probe_scratch_db_b"]


def test_r2_alter_database_owner_without_a_membership_grant_can_simply_work():
    summary, _ = probe_job.run_job(job_env(), FakeDb())
    steps = steps_of(summary)
    assert steps["alter_scratch_database_owner_without_membership_grant"]["outcome"] == "ok"
    assert steps["alter_scratch_database_owner_after_membership_grant"]["outcome"] == "ok"
    assert summary["r2_findings"]["alter_database_owner_without_membership_grant"] == "ok"


def test_r2_grant_with_set_true_refused_falls_back_to_the_plain_grant():
    refuse = DbError("42601", 'syntax error at or near "SET"')
    db = FakeDb(fail={"WITH SET TRUE": refuse})
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    assert steps["grant_migrator_to_admin_with_set_true"]["outcome"] == "error"
    assert steps["grant_migrator_to_admin_plain"]["outcome"] == "ok"


def test_r2_create_schema_if_not_exists_fails_for_the_migrator_and_the_existence_check_works():
    db = FakeDb()
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    blocked = steps["migrator_create_schema_if_not_exists_existing_schema"]
    assert blocked["outcome"] == "error" and blocked["sqlstate"] == "42501"
    assert "permission denied for database" in blocked["error"] and blocked["as_expected"] is True
    assert steps["migrator_existence_check_then_skip"]["outcome"] == "ok"
    assert steps["migrator_existence_check_then_skip"]["rows"] == [["1"]]
    assert steps["migrator_creates_table_in_existing_schema"]["outcome"] == "ok"
    other = steps["migrator_create_other_schema_expect_error"]
    assert other["outcome"] == "error" and other["sqlstate"] == "42501" and other["as_expected"] is True
    assert summary["r2_findings"]["create_schema_if_not_exists_on_existing_schema_as_migrator"] == "error"


def test_r2_a_migrator_that_can_create_schemas_is_reported_as_unexpected_and_cleaned_up():
    db = FakeDb(migrator_has_create=True)
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    assert steps["migrator_create_schema_if_not_exists_existing_schema"]["outcome"] == "ok"
    assert steps["migrator_create_schema_if_not_exists_existing_schema"]["as_expected"] is False
    assert steps["migrator_create_other_schema_expect_error"]["outcome"] == "ok"
    assert steps["migrator_create_other_schema_expect_error"]["as_expected"] is False
    assert steps["precondition_migrator_connect_yes_create_no"]["as_expected"] is False
    assert summary["r2_findings"]["preconditions_hold"] is False
    assert db.other_schemas == set()  # the cleanup step removed the unwanted schema
    assert "migrator_create_schema_if_not_exists_existing_schema" in summary["bootstrap_summary"]["unexpected"]


def test_r2_preconditions_flag_a_schema_the_migrator_does_not_own():
    db = FakeDb(fail={"ALTER SCHEMA": DbError("42501", "must be owner of schema probe_scratch")})
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    assert steps["precondition_schema_owned_by_migrator"]["as_expected"] is False
    assert summary["r2_findings"]["preconditions_hold"] is False


def test_r2_dependent_steps_are_skipped_not_faked_when_the_migrator_cannot_log_in():
    grant = 'GRANT CONNECT ON DATABASE "probe" TO "probe_migrator"'
    db = FakeDb(connect_required=True, fail={grant: DbError("42501", "permission denied")})
    summary, _ = probe_job.run_job(job_env(), db)
    steps = steps_of(summary)
    assert steps["login_as_probe_migrator_after_connect_grant"]["outcome"] == "error"
    for step in ("migrator_create_schema_if_not_exists_existing_schema", "migrator_existence_check_then_skip",
                 "migrator_creates_table_in_existing_schema", "migrator_create_other_schema_expect_error"):
        assert steps[step]["outcome"] == "skipped" and steps[step]["as_expected"] is False
    assert summary["marker"]["written"] is True


@pytest.mark.parametrize("options", [{}, {"alter_db_needs_membership": True}, {"connect_required": True}])
def test_r2_rerun_is_idempotent_and_leaves_no_scratch_state(options):
    db = FakeDb(**options)
    env = job_env()
    runs = [probe_job.run_job(env, db)[0] for _ in range(3)]
    first = steps_of(runs[0])
    for later in runs[1:]:
        later_steps = steps_of(later)
        for step in R2_NEW_STEPS:
            assert later_steps[step]["outcome"] == first[step]["outcome"], step
        # The CONNECT grant persists, so from the second run on the original migrator login works.
        stable = [n for n in runs[0]["bootstrap_summary"]["unexpected"] if n != "login_as_probe_migrator"]
        assert later["bootstrap_summary"]["unexpected"] == stable
    assert db.databases == {"probe": "doadmin"} and db.other_schemas == set()
    assert [r["marker"]["run_number"] for r in runs] == [1, 2, 3]
    # A re-run starts by clearing a scratch database that an earlier interrupted run left behind.
    db.databases["probe_scratch_db_a"] = "probe_migrator"
    again = probe_job.run_job(env, db)[0]
    assert steps_of(again)["create_scratch_database_a"]["outcome"] == "ok"
    assert db.databases == {"probe": "doadmin"}


def test_r2_new_steps_never_show_a_password_and_scrub_errors():
    password = rand_secret("pw")
    leak = DbError("42501", f"permission denied (user doadmin password={password} "
                            "host db-host-1.d.db.ondigitalocean.com)")
    db = FakeDb(fail={"CREATE DATABASE": leak, "CREATE SCHEMA IF NOT EXISTS": leak,
                      "ALTER TABLE": leak})
    summary, code = probe_job.run_job(job_env(password=password), db)
    text = json.dumps(summary)
    assert code == 0 and password not in text and "db-host-1" not in text
    for role_password in db.roles.values():
        assert role_password not in text
    created = [s for s in summary["bootstrap"] if s["step"].startswith("create_scratch_database")]
    assert created and all(s["sqlstate"] == "42501" and password not in s["error"] for s in created)
    new = [s for s in summary["bootstrap"] if s["step"] in R2_NEW_STEPS]
    assert all("PASSWORD" not in s.get("statement", "") for s in new)
    assert summary["marker"]["extended_columns"] is False  # the ALTER TABLE failure was tolerated


# ------------------------------------------------------- R3: retry versus rollback markers


def test_clean_tag_accepts_plain_labels_only():
    assert probe_job.clean_tag(None) == "(none)" and probe_job.clean_tag("  ") == "(none)"
    assert probe_job.clean_tag("fail-1") == "fail-1" and probe_job.clean_tag("ok_1.2") == "ok_1.2"
    assert probe_job.clean_tag("bad tag!") == "(invalid)"
    assert probe_job.clean_tag("x" * 41) == "(invalid)"
    assert probe_job.clean_tag("x" * 40) == "x" * 40


def test_platform_identifiers_list_names_and_record_only_plain_non_secret_values():
    token_like = rand_secret("") + rand_secret("")
    uuid_value = "0f8fad5b-d9cb-469f-a165-70867728950e"
    env = {"APP_DEPLOYMENT_ID": uuid_value, "HOSTNAME": "probe-job-7d9f5-abcde",
           "BUILD_REF": token_like, "COMPONENT_URL": "https://example.invalid/path",
           "DEPLOYMENT_TOKEN": "short-but-sensitive", "PROBE_JOB_TAG": "fail-1",
           "PATH": "/usr/bin", "INSTANCE_PASSWORD": "pw"}
    found = probe_job.platform_identifiers(env)
    assert found["values"] == {"APP_DEPLOYMENT_ID": uuid_value, "HOSTNAME": "probe-job-7d9f5-abcde"}
    assert set(found["withheld"]) == {"BUILD_REF", "COMPONENT_URL"}
    assert "DEPLOYMENT_TOKEN" not in found["names_found"] and "INSTANCE_PASSWORD" not in found["names_found"]
    assert "PROBE_JOB_TAG" not in found["names_found"] and "PATH" not in found["names_found"]
    assert token_like not in json.dumps(found) and "example.invalid" not in json.dumps(found)


def test_marker_rows_carry_tag_mode_attempt_id_start_time_and_platform_ids():
    db = FakeDb()
    env = job_env(extra={"PROBE_JOB_TAG": "fail-1", "PROBE_JOB_FAIL": "true",
                         "APP_DEPLOYMENT_ID": "0f8fad5b-d9cb-469f-a165-70867728950e"})
    summary, code = probe_job.run_job(env, db)
    assert code == probe_job.EXIT_FAIL_MODE
    row = db.marker[-1]
    assert row[1] == "fail" and row[3] == "fail-1"
    assert re.fullmatch(r"[0-9a-f]{16}", row[4]) and row[4] == summary["attempt_id"]
    assert row[5] == summary["started_at"] and "0f8fad5b" in row[6]
    assert summary["job_tag"] == "fail-1" and summary["marker"]["extended_columns"] is True
    assert summary["variables"]["expected_present"]["PROBE_JOB_TAG"] is True
    other, _ = probe_job.run_job(env, db)
    assert other["attempt_id"] != summary["attempt_id"]  # one random id per attempt


def test_main_logs_the_attempt_before_anything_else(capsys):
    db = FakeDb()
    probe_job.main(job_env(extra={"PROBE_JOB_TAG": "ok-1"}), db)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["event"] == "probe_job_attempt_started"
    assert lines[0]["job_tag"] == "ok-1" and lines[0]["job_mode"] == "normal"
    assert lines[-1]["event"] == "probe_job_summary" and lines[-1]["attempt_id"] == lines[0]["attempt_id"]


def test_client_groups_marker_rows_by_tag_and_mode_and_orders_the_attempts():
    db = FakeDb()
    ok = job_env(extra={"PROBE_JOB_TAG": "ok-1"})
    fail = job_env(extra={"PROBE_JOB_TAG": "fail-1", "PROBE_JOB_FAIL": "true"})
    for env in (ok, fail, fail, ok):  # a normal deployment, a failing job tried twice, a rollback re-run
        probe_job.run_job(env, db)
    now = probe_client.OrderingObserver(db).read_now()
    assert now["found"] is True and now["rows"] == 4 and now["tag_columns"] is True
    assert now["by_tag_mode"] == [{"tag": "fail-1", "mode": "fail", "count": 2},
                                  {"tag": "ok-1", "mode": "normal", "count": 2}]
    assert [(a["tag"], a["mode"]) for a in now["attempts"]] == [
        ("ok-1", "normal"), ("fail-1", "fail"), ("fail-1", "fail"), ("ok-1", "normal")]
    assert all(a["started_at"].startswith("20") for a in now["attempts"])
    assert len({a["attempt_id"] for a in now["attempts"]}) == 4
    assert [a["run_number"] for a in now["attempts"]] == [1, 2, 3, 4]


def test_client_results_endpoint_serves_a_live_marker_read():
    import http.client
    from http.server import ThreadingHTTPServer

    db = FakeDb()
    token = rand_secret("t")
    observer = probe_client.OrderingObserver(db)
    state = probe_client.ProbeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), probe_client.make_handler(state, observer, token))
    host, port = serve(server)

    def results() -> dict:
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/results", headers={"Authorization": f"Bearer {token}"})
        data = conn.getresponse().read()
        conn.close()
        return json.loads(data)

    try:
        assert results()["marker_now"]["found"] is False
        probe_job.run_job(job_env(extra={"PROBE_JOB_TAG": "ok-1"}), db)
        probe_job.run_job(job_env(extra={"PROBE_JOB_TAG": "fail-1", "PROBE_JOB_FAIL": "true"}), db)
        live = results()["marker_now"]
        assert [(g["tag"], g["count"]) for g in live["by_tag_mode"]] == [("fail-1", 1), ("ok-1", 1)]
        assert results()["ordering"]["first_request"]["marker"]["found"] is False  # observed once, earlier
    finally:
        server.shutdown()
        server.server_close()


def test_read_now_without_a_database_and_on_errors():
    assert probe_client.OrderingObserver(None).read_now()["found"] is None

    class Broken:
        def connect(self, **kwargs):
            raise DbError("28P01", "password authentication failed")

    assert probe_client.OrderingObserver(Broken()).read_now()["sqlstate"] == "28P01"


def test_old_marker_table_keeps_working_and_old_rows_group_as_none():
    db = FakeDb(legacy_marker=True)
    db.marker.append((1, "normal", "1, 'normal'", None, None, None, None))  # written by the first probe
    summary, _ = probe_job.run_job(job_env(extra={"PROBE_JOB_TAG": "ok-1"}), db)
    assert summary["marker"]["written"] is True and summary["marker"]["extended_columns"] is True
    assert summary["marker"]["run_number"] == 2
    now = probe_client.OrderingObserver(db).read_now()
    assert now["by_tag_mode"] == [{"tag": "(none)", "mode": "normal", "count": 1},
                                  {"tag": "ok-1", "mode": "normal", "count": 1}]
    assert now["attempts"][0]["attempt_id"] == ""


def test_marker_falls_back_to_the_old_shape_when_columns_cannot_be_added():
    db = FakeDb(legacy_marker=True, alter_denied=True)
    summary, code = probe_job.run_job(job_env(extra={"PROBE_JOB_TAG": "ok-1"}), db)
    assert code == 0 and summary["marker"]["written"] is True
    assert summary["marker"]["extended_columns"] is False
    assert any("could not add tag columns" in e for e in summary["marker"]["errors_before_success"])
    assert db.marker[-1][3] is None
    now = probe_client.OrderingObserver(db).read_now()
    assert now["found"] is True and now["rows"] == 1 and now["tag_columns"] is False


# ------------------------------------------------- R1: the outsider's database denial check


def pg_like_server(mode: str, cert: str | None = None, key: str | None = None,
                   tmp: Path | None = None):
    """A loopback listener that speaks just enough of the PostgreSQL start-up to test the stages."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    seen: list[bytes] = []

    def run() -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            try:
                conn.settimeout(3)
                if mode == "silent":
                    conn.recv(8)
                    time.sleep(2)
                    continue
                request = conn.recv(8)
                seen.append(request)
                if mode == "close":
                    continue
                if mode == "no_tls":
                    conn.sendall(b"N")
                    continue
                conn.sendall(b"S")
                if mode == "tls_handshake_fails":
                    continue  # hang up instead of handshaking
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.minimum_version = ssl.TLSVersion.TLSv1_2
                assert tmp is not None and cert and key
                (tmp / "c.pem").write_text(cert)
                (tmp / "k.pem").write_text(key)
                context.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
                try:
                    with context.wrap_socket(conn, server_side=True):
                        pass
                except (ssl.SSLError, OSError):
                    pass
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=run, daemon=True).start()
    return listener, listener.getsockname()[1], seen


class FakeLoginGateway:
    """Stands in for PsycopgGateway at the login stage."""

    def __init__(self, error: DbError | None = None) -> None:
        self.error, self.calls = error, []

    def __call__(self, info):
        self.info = info
        return self

    def connect(self, *, sslmode, sslrootcert=None, user=None, password=None):
        self.calls.append((sslmode, sslrootcert, sslrootcert and os.path.exists(sslrootcert)))
        if self.error is not None:
            raise self.error
        return type("S", (), {"execute": lambda self, sql: [(1,)], "close": lambda self: None})()


@pytest.fixture()
def db_creds():
    return {"user": rand_secret("u"), "password": rand_secret("pw")}


def run_db_probe(port: int, creds: dict, gateway: FakeLoginGateway | None = None, **kwargs):
    return probe_outsider.probe_database(
        "127.0.0.1", port, "defaultdb", creds["user"], creds["password"], timeout=1.0,
        gateway_factory=gateway or FakeLoginGateway(), **kwargs)


def assert_no_secret(result: dict, creds: dict) -> None:
    text = json.dumps(result)
    assert creds["user"] not in text and creds["password"] not in text and "127.0.0.1" not in text


def test_db_check_classifies_connection_refused(db_creds):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    result = run_db_probe(closed, db_creds)
    assert result["classification"] == "connection_refused" and result["stopped_at"] == "tcp"
    assert result["reached_database"] is False and result["stages"]["dns"]["ok"] is True
    assert result["stages"]["tls"] is None and result["stages"]["login"] is None
    assert_no_secret(result, db_creds)


def test_db_check_classifies_a_tcp_timeout_and_other_tcp_errors(db_creds):
    def slow(*args, **kwargs):
        raise TimeoutError("timed out")

    def unreachable(*args, **kwargs):
        raise OSError(65, "No route to host")

    result = run_db_probe(1, db_creds, create_connection=slow)
    assert result["classification"] == "timeout" and result["stopped_at"] == "tcp"
    other = run_db_probe(1, db_creds, create_connection=unreachable)
    assert other["classification"] == "tcp_error" and "No route" in other["stages"]["tcp"]["reason"]
    assert_no_secret(result, db_creds)


def test_db_check_classifies_dns_failure(db_creds):
    result = probe_outsider.probe_database("no-such-host.invalid", 25060, "defaultdb", db_creds["user"],
                                           db_creds["password"], timeout=1.0)
    assert result["classification"] == "dns_failure" and result["stopped_at"] == "dns"
    assert db_creds["password"] not in json.dumps(result)


@pytest.mark.parametrize("mode,expected", [("no_tls", "tls_not_offered"), ("close", "tls_failed"),
                                           ("silent", "tls_failed"), ("tls_handshake_fails", "tls_failed")])
def test_db_check_classifies_tls_stage_failures(mode, expected, db_creds):
    listener, port, seen = pg_like_server(mode)
    try:
        result = run_db_probe(port, db_creds)
    finally:
        listener.close()
    assert result["classification"] == expected and result["stopped_at"] == "tls"
    assert result["stages"]["tcp"] == {"ok": True} and result["stages"]["login"] is None
    assert seen == [probe_outsider.POSTGRES_SSL_REQUEST] or mode == "silent"
    assert_no_secret(result, db_creds)


def test_db_check_tls_ok_then_authentication_refused(db_creds, tmp_path):
    cert, key = make_cert(["127.0.0.1"])
    listener, port, _ = pg_like_server("tls", cert, key, tmp_path)
    try:
        for sqlstate in ("28P01", "28000"):
            gateway = FakeLoginGateway(DbError(sqlstate, f"password authentication failed for user "
                                                         f"{db_creds['user']} password={db_creds['password']} "
                                                         "at 127.0.0.1"))
            result = run_db_probe(port, db_creds, gateway)
            assert result["classification"] == "tls_ok_authentication_refused"
            assert result["stopped_at"] == "login" and result["stages"]["tls"]["ok"] is True
            assert result["stages"]["tls"]["tls_version"].startswith("TLS")
            assert result["stages"]["login"]["sqlstate"] == sqlstate and result["reached_database"] is False
            assert gateway.calls == [("require", None, None)]
            assert_no_secret(result, db_creds)
    finally:
        listener.close()


def test_db_check_tls_ok_then_other_login_failures_and_missing_driver(db_creds, tmp_path):
    cert, key = make_cert(["127.0.0.1"])
    listener, port, _ = pg_like_server("tls", cert, key, tmp_path)
    try:
        other = run_db_probe(port, db_creds, FakeLoginGateway(DbError("3D000", "database does not exist")))
        assert other["classification"] == "login_failed_other" and other["stages"]["login"]["sqlstate"] == "3D000"
        none = run_db_probe(port, db_creds, FakeLoginGateway(DbError(None, "connection failed: closed")))
        assert none["classification"] == "login_failed_other"
        missing = run_db_probe(port, db_creds,
                               FakeLoginGateway(DbError(None, "psycopg is not installed in this image")))
        assert missing["classification"] == "login_not_run"
    finally:
        listener.close()


def test_db_check_success_is_reported_as_reached(db_creds, tmp_path):
    cert, key = make_cert(["127.0.0.1"])
    listener, port, _ = pg_like_server("tls", cert, key, tmp_path)
    try:
        result = run_db_probe(port, db_creds)
    finally:
        listener.close()
    assert result["classification"] == "login_ok_connected" and result["reached_database"] is True
    assert result["stopped_at"] is None and result["stages"]["login"] == {"ok": True}
    assert_no_secret(result, db_creds)


def test_db_check_uses_verify_full_with_a_ca_file_that_is_removed_afterwards(db_creds, tmp_path):
    cert, key = make_cert(["127.0.0.1"])
    listener, port, _ = pg_like_server("tls", cert, key, tmp_path)
    gateway = FakeLoginGateway()
    try:
        result = run_db_probe(port, db_creds, gateway, ca_pem=cert)
    finally:
        listener.close()
    mode, path, existed = gateway.calls[0]
    assert mode == "verify-full" and existed is True and not os.path.exists(path)
    assert result["login_sslmode"] == "verify-full"


def test_db_check_from_environment_not_configured_and_configured(db_creds, monkeypatch):
    assert probe_outsider.database_check({})["classification"] == "not_configured"
    partial = probe_outsider.database_check({"PROBE_OUTSIDER_DB_HOST": "h.example"})
    assert partial["classification"] == "not_configured" and "USER" in partial["note"]
    bad_port = probe_outsider.database_check({
        "PROBE_OUTSIDER_DB_HOST": "h.example", "PROBE_OUTSIDER_DB_USER": db_creds["user"],
        "PROBE_OUTSIDER_DB_PASSWORD": db_creds["password"], "PROBE_OUTSIDER_DB_PORT": "x"})
    assert bad_port["classification"] == "not_configured" and "PORT" in bad_port["note"]
    captured = {}

    def fake_probe(host, port, dbname, user, password, ca_pem=None, **kwargs):
        captured.update(host=host, port=port, dbname=dbname, ca=ca_pem)
        return {"check": "database_denial", "classification": "timeout", "reached_database": False}

    monkeypatch.setattr(probe_outsider, "probe_database", fake_probe)
    env = {"PROBE_OUTSIDER_DB_HOST": "h.example", "PROBE_OUTSIDER_DB_USER": db_creds["user"],
           "PROBE_OUTSIDER_DB_PASSWORD": db_creds["password"], "PROBE_OUTSIDER_DB_CA_PEM": "junk"}
    result = probe_outsider.database_check(env)
    assert captured == {"host": "h.example", "port": 25060, "dbname": "defaultdb", "ca": None}
    assert "PROBE_OUTSIDER_DB_CA_PEM" in result["ca_note"] and "junk" not in json.dumps(result)


def test_outsider_summary_carries_the_database_classification_without_secrets(db_creds, monkeypatch):
    def fake_probe(*args, **kwargs):
        return {"check": "database_denial", "classification": "tls_ok_authentication_refused",
                "reached_database": False}

    monkeypatch.setattr(probe_outsider, "probe_database", fake_probe)
    env = {"PROBE_OUTSIDER_DB_HOST": "h.example", "PROBE_OUTSIDER_DB_USER": db_creds["user"],
           "PROBE_OUTSIDER_DB_PASSWORD": db_creds["password"]}
    out = probe_outsider.run_outsider(env)
    assert out["summary"]["database_classification"] == "tls_ok_authentication_refused"
    assert out["summary"]["database_reached"] is False and out["summary"]["isolated"] is True
    text = json.dumps(out)
    assert db_creds["password"] not in text and db_creds["user"] not in text
    assert probe_outsider.run_outsider({})["summary"]["database_classification"] == "not_configured"


def test_laptop_script_maps_variables_sets_exit_codes_and_prints_no_secret(db_creds, monkeypatch, capsys):
    seen = {}
    outcome = {"c": "login_ok_connected", "r": True}

    def fake_probe(host, port, dbname, user, password, ca_pem=None, **kwargs):
        seen.update(host=host, port=port, dbname=dbname, user=user, password=password)
        return {"check": "database_denial", "classification": outcome["c"],
                "reached_database": outcome["r"], "host_redacted": "***"}

    monkeypatch.setattr(probe_outsider, "probe_database", fake_probe)
    env = {"PROBE_LAPTOP_DB_HOST": "h.example", "PROBE_LAPTOP_DB_USER": db_creds["user"],
           "PROBE_LAPTOP_DB_PASSWORD": db_creds["password"], "PROBE_LAPTOP_DB_PORT": "25061"}
    assert probe_laptop_dbcheck.main(env) == 1
    outcome.update(c="timeout", r=False)
    assert probe_laptop_dbcheck.main(env) == 0
    outcome.update(c="login_not_run", r=False)
    assert probe_laptop_dbcheck.main(env) == 2
    assert probe_laptop_dbcheck.main({}) == 2
    assert seen["port"] == 25061 and seen["dbname"] == "defaultdb" and seen["host"] == "h.example"
    printed = capsys.readouterr().out
    assert db_creds["password"] not in printed and db_creds["user"] not in printed


def test_laptop_script_against_a_closed_port_reports_refused(db_creds, capsys):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed = sock.getsockname()[1]
    env = {"PROBE_LAPTOP_DB_HOST": "127.0.0.1", "PROBE_LAPTOP_DB_PORT": str(closed),
           "PROBE_LAPTOP_DB_USER": db_creds["user"], "PROBE_LAPTOP_DB_PASSWORD": db_creds["password"]}
    assert probe_laptop_dbcheck.main(env) == 0
    out = capsys.readouterr().out
    assert json.loads(out)["classification"] == "connection_refused"
    assert db_creds["password"] not in out and "127.0.0.1" not in out


# --------------------------------------------------------------------- kit-wide properties


def test_every_ssl_context_in_the_kit_sets_a_minimum_tls_version():
    for path in PROBE_DIR.glob("*.py"):
        text = path.read_text()
        contexts = text.count("create_default_context(") + text.count("SSLContext(")
        assert contexts <= text.count("minimum_version ="), f"{path.name}: a context without minimum_version"


def test_retest_files_specs_and_dockerfile():
    names = {p.name for p in PROBE_DIR.iterdir()}
    assert "probe_laptop_dbcheck.py" in names
    docker = (PROBE_DIR / "Dockerfile.probe").read_text()
    assert "probe_laptop_dbcheck" not in docker  # the laptop script never goes into the image
    app = (PROBE_DIR / "probe-app.yaml").read_text()
    job_block = app.split("- name: probe-job", 1)[1]
    assert "key: PROBE_JOB_TAG" in job_block and 'value: "ok-1"' in job_block
    outsider = (PROBE_DIR / "probe-outsider-app.yaml").read_text()
    for name in ("PROBE_OUTSIDER_DB_HOST", "PROBE_OUTSIDER_DB_USER", "PROBE_OUTSIDER_DB_PASSWORD"):
        block = outsider.split(f"key: {name}", 1)[1].split("- key:", 1)[0]
        assert "type: SECRET" in block and 'value: ""' in block
    assert "defaultdb" in outsider


def test_runbook_documents_the_retest_and_every_new_step_name():
    text = (PROBE_DIR / "RUNBOOK.md").read_text()
    assert "Retest (2026-11-12 follow-up)" in text
    for step in R2_NEW_STEPS:
        assert step in text, f"RUNBOOK.md does not mention step {step}"
    for word in ("fail-1", "ok-1", "PROBE_JOB_TAG", "marker_now", "probe_laptop_dbcheck.py",
                 "PROBE_OUTSIDER_DB_HOST", "tls_ok_authentication_refused", "scratch"):
        assert word in text
    assert "db-s-1vcpu-1gb" in text and "no `--format`" in text and "HTTP 400" in text
