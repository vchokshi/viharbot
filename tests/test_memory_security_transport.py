"""Memory service security matrix, part 2 (Part of #40): pinned certificate behavior, certificate
expiry and rotation assumptions, and fail-open memory availability WITHOUT fail-open authentication.

Offline: the only network is loopback servers started here. Certificates and keys are generated at
run time; no secret-looking literal is written in this file.
"""

from __future__ import annotations

import base64
import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest
from charlie_memory_helpers import (
    FakeMemoryHttp,
    FakeRepo,
    LogCapture,
    charlie,
    cleanup_module,
    memory_env,
    serve,
)
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from memory_helpers import ExplodingRepository, agent_path, build_world, new_key

from viharbot import charlie_memory
from viharbot import memory_client as mc
from viharbot.memory_service import stage_entry as se
from viharbot.memory_service.app import create_app_from_environment
from viharbot.memory_service.repository import MemoryStoreError
from viharbot.service_auth import ServiceAuthConfigError, b64url_encode

LIST = b'{"kind":"memory_record_list","not_instructions":true,"records":[]}'


# --- certificate material -------------------------------------------------------------------------


def make_cert(*, start: dt.timedelta, end: dt.timedelta, names=("memory",)):
    """A self-signed server certificate valid from now+start to now+end (negative = in the past)."""
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now + start).not_valid_after(now + end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    return (cert.public_bytes(serialization.Encoding.PEM).decode(),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode())


def valid_cert(**kw):
    return make_cert(start=dt.timedelta(minutes=-5), end=dt.timedelta(days=90), **kw)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


class TlsServer:
    """A loopback HTTPS server that answers every request with an empty record list."""

    def __init__(self, tmp_path: Path, cert_pem: str, key_pem: str) -> None:
        cert, key = tmp_path / "srv.crt", tmp_path / "srv.key"
        cert.write_text(cert_pem)
        key.write_text(key_pem)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(str(cert), str(key))
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                raw, _ = self.sock.accept()
            except OSError:
                return
            try:
                with self.context.wrap_socket(raw, server_side=True) as conn:
                    data = b""
                    while b"\r\n\r\n" not in data:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    self.requests += 1
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n"
                                 b"Content-Length: " + str(len(LIST)).encode() + b"\r\n\r\n" + LIST)
            except (ssl.SSLError, OSError):
                pass

    def close(self) -> None:
        self.sock.close()


@pytest.fixture(autouse=True)
def _stub_signing_and_clean(monkeypatch):
    """Transport tests do not need real assertions; the signing tests below undo this."""
    yield
    cleanup_module()


@pytest.fixture
def stub_signing(monkeypatch):
    monkeypatch.setattr(mc, "outbound_headers", lambda *a, **k: {"X-Viharbot-Assertion": "test"})


def pinned_client(cert_pem: str, port: int, host: str = "memory"):
    client = mc.client_from_environment({mc.URL_ENV: f"https://{host}:{port}", mc.CA_PEM_ENV: b64(cert_pem)})
    client._resolve = staticmethod(lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))])
    return client


# --- pinned certificate: expiry, rotation, trust anchor --------------------------------------------


def test_an_expired_pinned_certificate_fails_closed_and_nothing_is_sent(tmp_path, stub_signing):
    cert, key = make_cert(start=dt.timedelta(days=-400), end=dt.timedelta(days=-1))
    server = TlsServer(tmp_path, cert, key)
    try:
        with pytest.raises(mc.MemoryClientError) as caught:
            pinned_client(cert, server.port).recent("req-expired")
        assert caught.value.reason == "tls_verification_failed"
        assert server.requests == 0  # the request, and so the assertion, was never transmitted
    finally:
        server.close()


def test_a_not_yet_valid_pinned_certificate_fails_closed(tmp_path, stub_signing):
    cert, key = make_cert(start=dt.timedelta(days=2), end=dt.timedelta(days=90))
    server = TlsServer(tmp_path, cert, key)
    try:
        with pytest.raises(mc.MemoryClientError) as caught:
            pinned_client(cert, server.port).recent("req-future")
        assert caught.value.reason == "tls_verification_failed" and server.requests == 0
    finally:
        server.close()


def test_rotation_requires_both_sides_to_change_and_a_stale_pin_fails_closed(tmp_path, stub_signing):
    old_cert, _ = valid_cert()
    new_cert, new_key = valid_cert()
    server = TlsServer(tmp_path, new_cert, new_key)  # the server already rotated
    try:
        with pytest.raises(mc.MemoryClientError) as caught:
            pinned_client(old_cert, server.port).recent("req-stale-pin")
        assert caught.value.reason == "tls_verification_failed" and server.requests == 0
        assert pinned_client(new_cert, server.port).recent("req-new-pin") == []  # new pin works
    finally:
        server.close()


def test_there_is_no_overlap_window_for_rotation_one_pin_only(stub_signing):
    """Assumption recorded in the findings: a PEM variable with two certificates is refused, so a
    rotation cannot trust old and new at once; it is a hard cut-over of both components."""
    one, _ = valid_cert()
    two, _ = valid_cert()
    with pytest.raises(mc.MemoryClientError) as caught:
        mc.client_from_environment({mc.URL_ENV: "https://memory:8443", mc.CA_PEM_ENV: b64(one + two)})
    assert caught.value.reason == "bad_ca_pem"


def test_a_pin_that_is_already_expired_is_accepted_at_start_and_only_fails_when_used(tmp_path):
    """Recorded gap (LOW): the client does not check the pin's validity at start-up, so Charlie logs
    `enabled` and memory is silently dead until the first call fails closed. Safe, but not loud."""
    cert, _ = make_cert(start=dt.timedelta(days=-400), end=dt.timedelta(days=-1))
    import logging

    memory_logger = logging.getLogger("memory_client")
    previous = memory_logger.level
    memory_logger.setLevel(logging.INFO)
    try:
        with LogCapture() as logs:
            client = charlie_memory.load_client(
                "synthesis", {mc.URL_ENV: "https://memory:8443", mc.CA_PEM_ENV: b64(cert)})
    finally:
        memory_logger.setLevel(previous)
    assert client is not None
    assert [e["reason"] for e in logs.memory_events("startup")] == ["enabled"]


def test_the_system_trust_store_is_never_consulted_when_a_certificate_is_pinned():
    cert, _ = valid_cert()
    context = mc.client_from_environment({mc.URL_ENV: "https://memory:8443", mc.CA_PEM_ENV: b64(cert)})._context
    assert context.cert_store_stats()["x509"] == 1
    assert context.check_hostname is True and context.verify_mode == ssl.CERT_REQUIRED
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize("url", [
    "http://memory:8000", "http://10.0.0.5:8000", "http://192.168.1.4:8000", "http://[::1]:8000",
    "https://user:pw@memory:8443", "https://memory:8443/x", "https://memory:8443?a=b", "ftp://memory",
])
def test_plain_http_or_credentials_in_the_url_never_start_the_client(url):
    with pytest.raises(mc.MemoryClientError) as caught:
        mc.client_from_environment({mc.URL_ENV: url})
    assert caught.value.reason in ("insecure_transport", "bad_url")


def test_redirects_are_never_followed_and_carry_no_token_elsewhere(stub_signing):
    with FakeMemoryHttp(status=307, headers={"Location": "http://127.0.0.1:1/steal"}) as server:
        client = mc.client_from_environment(
            {mc.URL_ENV: server.url, mc.LOCAL_DEV_ENV: "on"})
        with pytest.raises(mc.MemoryClientError) as caught:
            client.recent("req-redirect")
    assert caught.value.reason == "http_307" and len(server.seen) == 1


# --- Stage launcher: certificate expiry assumptions ---------------------------------------------------


def _registry_text() -> str:
    raw = new_key()[1]
    return (f'[[keys]]\nkid = "k0"\nsub = "charlie"\npublic_key = "{raw}"\n'
            'capabilities = ["memory.read", "memory.write"]\n')


def _stage_env(cert_pem: str, key_pem: str) -> dict[str, str]:
    return {
        se.ROLE_ENV: "serve", se.CERT_ENV: b64(cert_pem), se.KEY_ENV: b64(key_pem),
        se.REGISTRY_ENV: b64(_registry_text()), "MEMORY_DB_HOST": "db.example.invalid",
        "MEMORY_DB_NAME": "memorydb", "MEMORY_DB_USER": "memory_service", "MEMORY_DB_PASSWORD": "x" * 24,
        se.DB_CA_ENV: b64(valid_cert()[0]), "PORT": "8443",
    }


def test_the_stage_launcher_starts_with_an_expired_certificate_but_says_so_loudly(capsys):
    """Recorded behavior (LOW, by design): expiry is a WARNING, not a refusal. The server comes up,
    Charlie then fails closed on TLS, memory is off, health stays green. Fail-open by availability."""
    cert, key = make_cert(start=dt.timedelta(days=-400), end=dt.timedelta(days=-2))
    served = []
    code = se.main(_stage_env(cert, key), server=lambda app, m, h, p: served.append(m.days_to_expiry),
                   app_factory=lambda env, conninfo: object())
    out = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert code == 0 and served and served[0] < 0
    warning = [line for line in out if line["event"] == "memory_stage_tls_certificate_expiring"]
    assert warning and warning[0]["expired"] is True and warning[0]["level"] == "warning"


def test_the_stage_launcher_refuses_a_key_that_does_not_match_the_certificate(capsys):
    cert, _ = valid_cert()
    _, other_key = valid_cert()
    assert se.main(_stage_env(cert, other_key), server=lambda *a: None, app_factory=lambda *a: object()) == se.EXIT_REFUSED
    assert "does not match" in capsys.readouterr().err


def test_the_stage_launcher_never_serves_plaintext_or_without_a_registry(capsys):
    cert, key = valid_cert()
    for name in (se.REGISTRY_ENV, se.CERT_ENV, se.KEY_ENV):
        env = _stage_env(cert, key)
        env.pop(name)
        served = []
        code = se.main(env, server=lambda *a: served.append(1), app_factory=lambda *a: object())
        assert code == se.EXIT_REFUSED and not served, name
    capsys.readouterr()


def test_the_database_connection_is_forced_to_verify_full_in_stage_mode():
    from viharbot.memory_service.app import conninfo_from_environment

    env = {"MEMORY_DB_HOST": "db.example.invalid", "MEMORY_DB_NAME": "n", "MEMORY_DB_USER": "u",
           "MEMORY_DB_PASSWORD": "p" * 20, "MEMORY_DB_SSLMODE": "disable"}
    conninfo = conninfo_from_environment(env, sslrootcert="/tmp/ca.pem", strict_tls=True)
    assert "sslmode=verify-full" in conninfo and "sslmode=disable" not in conninfo
    with pytest.raises(ServiceAuthConfigError):  # a remote host with no CA cannot downgrade to prefer
        conninfo_from_environment(env, strict_tls=True)


# --- fail-open memory availability, fail-closed authentication ----------------------------------------


def test_an_unreachable_memory_service_never_stops_charlie_reading_or_writing_around_it(stub_signing):
    client = mc.client_from_environment({mc.URL_ENV: "http://127.0.0.1:9", mc.LOCAL_DEV_ENV: "on",
                                         mc.TIMEOUT_ENV: "0.3"})
    assert charlie_memory.prior_block(client, "req-down", "synthesis") == ""
    result = type("R", (), {"confidence": "high", "key_points": ["a"], "caveats": []})()
    assert charlie_memory.store(client, {"research": {}}, result, "req-down", "synthesis") is None


def test_a_denied_call_is_swallowed_for_availability_but_is_not_retried_or_downgraded(stub_signing):
    with FakeMemoryHttp(status=403, body=b'{"detail":"forbidden"}') as server:
        client = mc.client_from_environment({mc.URL_ENV: server.url, mc.LOCAL_DEV_ENV: "on"})
        with LogCapture() as logs:
            assert charlie_memory.prior_block(client, "req-denied", "synthesis") == ""
        assert len(server.seen) == 1  # one attempt: no retry storm, no unsigned second try
        (event,) = logs.memory_events("read")
        assert event["decision"] == "skipped" and event["reason"] == "denied"
    with FakeMemoryHttp(status=401, body=b'{"detail":"x"}') as server:
        client = mc.client_from_environment({mc.URL_ENV: server.url, mc.LOCAL_DEV_ENV: "on"})
        result = type("R", (), {"confidence": "low", "key_points": [], "caveats": []})()
        charlie_memory.store(client, {}, result, "req-denied-2", "synthesis")
        assert len(server.seen) == 1


def test_with_signing_off_no_request_is_ever_made_there_is_no_unsigned_fallback(monkeypatch):
    for name in ("VIHARBOT_OUTBOUND_AUTH", "VIHARBOT_SIGNING_KID", "VIHARBOT_SIGNING_KEY",
                 "VIHARBOT_SIGNING_KEY_FILE", "VIHARBOT_SERVICE_SUBJECT"):
        monkeypatch.delenv(name, raising=False)
    with FakeMemoryHttp(status=200, body=LIST) as server:
        client = mc.client_from_environment({mc.URL_ENV: server.url, mc.LOCAL_DEV_ENV: "on"})
        with pytest.raises(mc.MemoryClientError) as caught:
            client.recent("req-unsigned")
        assert caught.value.reason == "signing_unavailable"
        with pytest.raises(mc.MemoryClientError) as caught:
            client.write("anything", "req-unsigned")
        assert caught.value.reason == "signing_unavailable"
        assert server.seen == []


def test_the_client_refuses_to_run_as_any_subject_but_charlie(stub_signing):
    for subject in ("web", "nova", "Charlie", "charlie2", "charlie.x"):
        with pytest.raises(mc.MemoryClientError) as caught:
            mc.client_from_environment({mc.URL_ENV: "http://127.0.0.1:8000", mc.LOCAL_DEV_ENV: "on",
                                        "VIHARBOT_SERVICE_SUBJECT": subject})
        assert caught.value.reason == "wrong_subject", repr(subject)


def test_a_database_outage_is_a_503_for_the_authenticated_and_never_opens_authentication(tmp_path):
    class Down(ExplodingRepository):
        def ping(self):
            raise MemoryStoreError("unavailable")

        def list(self, *a, **k):
            raise MemoryStoreError("unavailable")

    world = build_world(tmp_path, Down(), extra_subs=("charlie",))
    assert world.client.get("/health").status_code == 503
    assert world.call("charlie", "GET", agent_path("charlie")).status_code == 503
    # while the database is down, an unauthenticated or forged call is STILL 401/403, not 503/200
    assert world.client.get(agent_path("charlie")).status_code == 401
    assert world.call("charlie", "GET", agent_path("nova")).status_code == 403
    token = world.token("charlie", "GET", agent_path("charlie"))
    assert world.call(None, "GET", agent_path("charlie") + "?limit=2", token=token).status_code == 401


def test_the_memory_service_has_no_unauthenticated_mode(monkeypatch, tmp_path):
    for env in ({}, {"VIHARBOT_KEY_REGISTRY": ""}, {"VIHARBOT_KEY_REGISTRY": str(tmp_path / "missing.toml")},
                {"VIHARBOT_SERVICE_AUTH": "off"}):
        with pytest.raises(ServiceAuthConfigError):
            create_app_from_environment({**env, "MEMORY_DB_HOST": "h"})
    empty = tmp_path / "empty.toml"
    empty.write_text("")
    with pytest.raises(ServiceAuthConfigError):
        create_app_from_environment({"VIHARBOT_KEY_REGISTRY": str(empty)})


def test_charlie_answers_normally_when_memory_rejects_its_key_end_to_end(monkeypatch, tmp_path):
    """The real memory app over loopback, a registry that does not list Charlie's key: every memory
    call is refused (authentication did not fail open) and Charlie still answers 200."""
    repo = FakeRepo()
    world = build_world(tmp_path, repo)  # nova, scout, cipher, mallory: no charlie
    _, _public, seed = new_key()
    seed_file = tmp_path / "charlie.seed"
    seed_file.write_text(seed)
    with serve(world.client.app) as url:
        env = memory_env(url, str(seed_file), kid="charlie-test")
        with LogCapture() as logs:
            seen = charlie(monkeypatch, tmp_path, env)
            response = seen.ask("How should we proceed?", {"research": {"claim": "c"}})
    assert response.status_code == 200
    assert repo.records == []  # nothing was stored for an unauthenticated writer
    reasons = {e["reason"] for e in logs.memory_events() if e["operation"] in ("read", "write")}
    assert reasons == {"denied"}


def test_charlie_with_a_registered_key_does_store_and_the_control_works(monkeypatch, tmp_path):
    repo = FakeRepo()
    world = build_world(tmp_path, repo, extra_subs=("charlie",))
    with serve(world.client.app) as url:
        env = memory_env(url, str(world.keys["charlie"][1]), kid=world.keys["charlie"][0])
        seen = charlie(monkeypatch, tmp_path, env)
        assert seen.ask("How should we proceed?", {"research": {"claim": "c"}}).status_code == 200
        deadline = time.time() + 5
        while not repo.records and time.time() < deadline:
            time.sleep(0.05)
    assert len(repo.records) == 1 and repo.records[0].owner_agent_id == "charlie"
