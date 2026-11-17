"""Charlie's memory client trusting one pinned certificate from VIHARBOT_MEMORY_CA_PEM (Stage).

Offline: the only network is a loopback TLS server started here. Certificates are generated at run
time with the Stage material generator, so no key or certificate literal lives in the repository.
"""

from __future__ import annotations

import base64
import importlib.util
import socket
import ssl
import threading
from pathlib import Path

import pytest
from charlie_memory_helpers import ROOT, FakeRepo, LogCapture, charlie, cleanup_module, memory_env
from memory_helpers import build_world

from viharbot import memory_client as mc

spec = importlib.util.spec_from_file_location("stage_memory_materials", ROOT / "scripts" / "stage_memory_materials.py")
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

LIST = b'{"kind":"memory_record_list","not_instructions":true,"records":[]}'


@pytest.fixture(autouse=True)
def _fresh_module():
    yield
    cleanup_module()


@pytest.fixture(autouse=True)
def _unsigned(monkeypatch):
    monkeypatch.setattr(mc, "outbound_headers", lambda *a, **k: {"X-Viharbot-Assertion": "test"})


def make_cert(names=("memory",)):
    cert_pem, key_pem = gen.generate_certificate(list(names))
    return cert_pem.decode(), key_pem.decode()


def b64(pem: str) -> str:
    return base64.b64encode(pem.encode()).decode()


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


@pytest.fixture
def server(tmp_path):
    cert_pem, key_pem = make_cert()
    srv = TlsServer(tmp_path, cert_pem, key_pem)
    yield srv, cert_pem
    srv.close()


def client_for(env_extra, port, host="memory"):
    client = mc.client_from_environment({mc.URL_ENV: f"https://{host}:{port}", **env_extra})
    client._resolve = staticmethod(lambda h, p, type=0: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", p))])
    return client


def build(env_extra):
    return mc.client_from_environment({mc.URL_ENV: "https://memory:8443", **env_extra})


def test_the_pinned_certificate_is_trusted_and_a_call_succeeds(server):
    srv, cert = server
    assert client_for({mc.CA_PEM_ENV: b64(cert)}, srv.port).recent("req-1") == []
    assert srv.requests == 1


def test_the_pinned_certificate_is_the_only_trust_anchor_and_hostname_checking_is_on():
    cert, _ = make_cert()
    context = build({mc.CA_PEM_ENV: b64(cert)})._context
    assert context.cert_store_stats()["x509"] == 1  # exactly one certificate: no system roots
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_a_different_certificate_for_the_same_name_fails_closed(server):
    srv, _ = server
    other, _ = make_cert()  # a different key, also for the name `memory`
    with pytest.raises(mc.MemoryClientError) as caught:
        client_for({mc.CA_PEM_ENV: b64(other)}, srv.port).recent("req-2")
    assert caught.value.reason == "tls_verification_failed"
    assert srv.requests == 0  # nothing was sent to the unverified server


def test_a_name_mismatch_fails_closed_even_with_the_right_certificate(tmp_path):
    cert, key = make_cert(("not-memory",))
    srv = TlsServer(tmp_path, cert, key)
    try:
        with pytest.raises(mc.MemoryClientError) as caught:
            client_for({mc.CA_PEM_ENV: b64(cert)}, srv.port, host="memory").recent("req-3")
        assert caught.value.reason == "tls_verification_failed"
        assert srv.requests == 0
    finally:
        srv.close()


def forms(pem: str) -> dict[str, str]:
    body = "".join(pem.strip().splitlines()[1:-1])
    encoded = b64(pem)
    return {
        "base64": encoded,
        "base64_with_line_breaks": "\n".join(encoded[i:i + 40] for i in range(0, len(encoded), 40)),
        "raw_pem": pem,
        "literal_backslash_n": pem.strip().replace("\n", "\\n"),
        "flattened_to_spaces": pem.strip().replace("\n", " "),
        "crlf": pem.replace("\n", "\r\n"),
        "unwrapped_body": f"-----BEGIN CERTIFICATE----- {body} -----END CERTIFICATE-----",
    }


@pytest.mark.parametrize("form", ["base64", "base64_with_line_breaks", "raw_pem", "literal_backslash_n",
                                  "flattened_to_spaces", "crlf", "unwrapped_body"])
def test_every_accepted_form_decodes_to_the_same_certificate_and_works(server, form):
    srv, cert = server
    value = forms(cert)[form]
    assert mc.decode_ca_pem(value) == mc.decode_ca_pem(cert)
    assert build({mc.CA_PEM_ENV: value}) is not None
    assert client_for({mc.CA_PEM_ENV: value}, srv.port).recent("req-4") == []


@pytest.mark.parametrize("value", [
    "not a certificate", "!!!!", base64.b64encode(b"hello").decode(),
    "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",  # a block whose body is not a certificate
    "-----BEGIN CERTIFICATE-----\nno footer",
])
def test_unusable_values_refuse_to_start_with_a_content_free_reason(value):
    with pytest.raises(mc.MemoryClientError) as caught:
        build({mc.CA_PEM_ENV: value})
    assert caught.value.reason == "bad_ca_pem"
    assert value not in str(caught.value) and caught.value.__cause__ is None


def test_a_private_key_or_two_certificates_are_refused():
    cert1, key = make_cert()
    cert2, _ = make_cert()
    for value in (key, cert1 + cert2, b64(cert1 + cert2)):
        with pytest.raises(mc.MemoryClientError) as caught:
            build({mc.CA_PEM_ENV: value})
        assert caught.value.reason == "bad_ca_pem"


def test_the_file_variable_still_works_unchanged(server, tmp_path):
    srv, cert = server
    path = tmp_path / "ca.pem"
    path.write_text(cert)
    assert client_for({mc.CA_FILE_ENV: str(path)}, srv.port).recent("req-5") == []
    garbage = tmp_path / "garbage.pem"
    garbage.write_text("not a certificate")
    for bad in (str(tmp_path / "absent.pem"), str(garbage)):
        with pytest.raises(mc.MemoryClientError) as caught:
            build({mc.CA_FILE_ENV: bad})
        assert caught.value.reason == "bad_ca_file"


def test_with_neither_variable_the_system_trust_store_is_still_used():
    context = build({})._context
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True


def test_both_set_and_identical_works_and_both_set_and_different_refuses(server, tmp_path):
    srv, cert = server
    other, _ = make_cert()
    path = tmp_path / "ca.pem"
    path.write_text(cert)
    assert client_for({mc.CA_FILE_ENV: str(path), mc.CA_PEM_ENV: b64(cert)}, srv.port).recent("req-6") == []
    path.write_text(other)
    with pytest.raises(mc.MemoryClientError) as caught:
        build({mc.CA_FILE_ENV: str(path), mc.CA_PEM_ENV: b64(cert)})
    assert caught.value.reason == "ca_conflict"
    # An unreadable file, or a bad variable, is also a refusal when both are set.
    with pytest.raises(mc.MemoryClientError) as caught:
        build({mc.CA_PEM_ENV: b64(cert), mc.CA_FILE_ENV: str(tmp_path / "absent.pem")})
    assert caught.value.reason == "bad_ca_file"
    with pytest.raises(mc.MemoryClientError) as caught:
        build({mc.CA_PEM_ENV: "junk", mc.CA_FILE_ENV: str(path)})
    assert caught.value.reason == "bad_ca_pem"


def test_the_http_refusals_are_unchanged_when_the_variable_is_set():
    cert, _ = make_cert()
    for url in ("http://10.0.0.5:8000", "http://memory:8000", "http://127.0.0.1:8000"):
        with pytest.raises(mc.MemoryClientError) as caught:
            mc.client_from_environment({mc.URL_ENV: url, mc.CA_PEM_ENV: b64(cert)})
        assert caught.value.reason == "insecure_transport"
    local = {mc.URL_ENV: "http://127.0.0.1:8000", mc.LOCAL_DEV_ENV: "on", mc.CA_PEM_ENV: b64(cert)}
    assert mc.client_from_environment(local) is not None


def test_charlie_keeps_answering_and_the_value_is_never_logged(monkeypatch, tmp_path):
    world = build_world(tmp_path, FakeRepo(), extra_subs=("charlie",))
    key_file = str(world.keys["charlie"][1])
    good, _ = make_cert()
    marker = "MARKER-GARBAGE-VALUE-7c1a"
    values = {"good_but_unreachable": b64(good), "garbage": marker + b64(good)[:40]}
    for label, value in values.items():
        cleanup_module()
        env = memory_env("https://memory:8443", key_file, VIHARBOT_MEMORY_TIMEOUT_SECONDS="0.3",
                         VIHARBOT_MEMORY_CA_PEM=value)
        env.pop("VIHARBOT_MEMORY_LOCAL_DEV")
        with LogCapture() as logs:
            seen = charlie(monkeypatch, tmp_path, env)
            response = seen.ask("How should we proceed?", {"research": {"claim": "c"}})
        assert response.status_code == 200, label  # Charlie still answers
        dump = logs.everything()
        for part in (value, good.splitlines()[1], b64(good)[20:60], marker):
            if label == "good_but_unreachable" and part == marker:
                continue
            assert part not in dump, label
        if label == "garbage":
            assert seen.module._memory is None
            (event,) = logs.memory_events("startup")
            assert event["decision"] == "refused" and event["reason"] == "bad_ca_pem"
