"""The Stage launcher for the memory service (docs/stage-memory-design.md, items 1, 2 and 4).

Offline and deterministic: no DigitalOcean, no database, no model. Certificates, keys, registries
and passwords are generated at run time; no secret-looking value is written in this file. The TLS
test serves on loopback with a throwaway certificate.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import secrets
import socket
import ssl
import stat
import threading
import time
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from fastapi import FastAPI

from viharbot.memory_service import stage_entry as se
from viharbot.memory_service.app import conninfo_from_environment
from viharbot.service_auth import ServiceAuthConfigError, b64url_encode

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------------------ material builders


def make_pair(days: float = 365, names=("memory", "localhost"), ips=("127.0.0.1",)):
    key = ec.generate_private_key(ec.SECP256R1())
    now = dt.datetime.now(dt.timezone.utc)
    sans = [x509.DNSName(n) for n in names] + [x509.IPAddress(__import__("ipaddress").ip_address(i)) for i in ips]
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    return cert_pem, key_pem


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def registry_text(count: int = 1) -> str:
    parts = []
    for index in range(count):
        raw = Ed25519PrivateKey.generate().public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        parts.append(
            f'[[keys]]\nkid = "k{index}"\nsub = "charlie"\npublic_key = "{b64url_encode(raw)}"\n'
            'capabilities = ["memory.read", "memory.write"]\n'
        )
    return "\n".join(parts)


@pytest.fixture
def material():
    cert_pem, key_pem = make_pair()
    return cert_pem, key_pem


@pytest.fixture
def password():
    return secrets.token_hex(16)


@pytest.fixture
def env(material, password):
    cert_pem, key_pem = material
    return {
        se.ROLE_ENV: "serve",
        se.CERT_ENV: b64(cert_pem),
        se.KEY_ENV: b64(key_pem),
        se.REGISTRY_ENV: b64(registry_text()),
        "MEMORY_DB_HOST": "db.example.invalid",
        "MEMORY_DB_PORT": "25060",
        "MEMORY_DB_NAME": "memorydb",
        "MEMORY_DB_USER": "memory_service",
        "MEMORY_DB_PASSWORD": password,
        "MEMORY_DB_SSLMODE": "verify-full",
        se.DB_CA_ENV: b64(make_pair()[0]),
        "PORT": "8443",
    }


class Recorder:
    """Stands in for the server and the app factory and records what the launcher handed them."""

    def __init__(self):
        self.served = None
        self.factory_calls = []
        self.registry_mode = None
        self.registry_dir_mode = None
        self.registry_path = None
        self.ca_path = None
        self.ca_mode = None

    def app_factory(self, env, conninfo):
        self.factory_calls.append((dict(env), conninfo))
        path = Path(env["VIHARBOT_KEY_REGISTRY"])
        self.registry_path = path
        self.registry_mode = stat.S_IMODE(path.stat().st_mode)
        self.registry_dir_mode = stat.S_IMODE(path.parent.stat().st_mode)
        match = re.search(r"sslrootcert=(\S+)", conninfo)
        if match:
            self.ca_path = Path(match.group(1))
            self.ca_mode = stat.S_IMODE(self.ca_path.stat().st_mode)
        return object()

    def server(self, app, material, host, port):
        self.served = (host, port, material.days_to_expiry)
        assert self.registry_path.exists()  # still there while serving (KeyRegistry re-reads it)


# --------------------------------------------------------------------------- PEM decoding


def test_every_documented_form_of_a_pem_variable_decodes_to_the_same_material(material):
    cert_pem, key_pem = material
    flattened = " ".join(cert_pem.strip().split("\n"))
    literal = cert_pem.strip().replace("\n", "\\n")
    forms = [b64(cert_pem), cert_pem, literal, flattened, "  " + b64(cert_pem) + "\n"]
    decoded = {tuple(se.decode_pem_variable(form, "X")) for form in forms}
    assert len(decoded) == 1
    assert se.decode_pem_variable(b64(key_pem), "X")[0][0] == "PRIVATE KEY"


@pytest.mark.parametrize("value", [None, "", "   ", "not base64 !!!", b64("hello, not a pem"), "-----BEGIN CERTIFICATE-----"])
def test_bad_pem_variables_are_refused_without_echoing_the_value(value):
    with pytest.raises(se.StageConfigError) as caught:
        se.decode_pem_variable(value, "MEMORY_TLS_CERT_PEM")
    assert "MEMORY_TLS_CERT_PEM" in str(caught.value)
    if value and value.strip():
        assert value.strip() not in str(caught.value)


def test_bad_base64_certificate_is_refused_by_the_launcher(env, capsys):
    env[se.CERT_ENV] = "####" + env[se.CERT_ENV][4:]
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
    assert recorder.served is None and recorder.factory_calls == []
    assert "MEMORY_TLS_CERT_PEM" in capsys.readouterr().err


def test_missing_key_or_certificate_is_refused(env):
    for name in (se.KEY_ENV, se.CERT_ENV):
        broken = {k: v for k, v in env.items() if k != name}
        recorder = Recorder()
        assert se.main(broken, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
        assert recorder.served is None


def test_a_key_that_does_not_match_the_certificate_is_refused(env, capsys):
    env[se.KEY_ENV] = b64(make_pair()[1])
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
    assert recorder.served is None
    assert "does not match" in capsys.readouterr().err


def test_an_encrypted_or_malformed_key_is_refused(env):
    key = ec.generate_private_key(ec.SECP256R1())
    encrypted = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(secrets.token_bytes(8)),
    ).decode()
    label = "PRIVATE" + " KEY"  # built at run time so the repository's key-material scan stays meaningful
    malformed = f"-----BEGIN {label}-----\nAAAA\n-----END {label}-----\n"
    for bad in (encrypted, malformed):
        env[se.KEY_ENV] = b64(bad)
        with pytest.raises(se.StageConfigError):
            se.load_tls_material(env)


def test_a_certificate_variable_that_holds_only_a_key_is_refused(env, material):
    env[se.CERT_ENV] = b64(material[1])
    with pytest.raises(se.StageConfigError, match="no certificate"):
        se.load_tls_material(env)


# -------------------------------------------------------------------------------- expiry


def test_days_to_expiry_and_fingerprint_are_logged_and_a_warning_when_under_thirty_days(capsys):
    cert_pem, key_pem = make_pair(days=10.5)
    material = se.load_tls_material({se.CERT_ENV: b64(cert_pem), se.KEY_ENV: b64(key_pem)})
    assert material.days_to_expiry == 10
    se.log_certificate_status(material)
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["level"] for line in lines] == ["info", "warning"]
    assert lines[0]["days_to_expiry"] == 10 and lines[0]["fingerprint_sha256"] == material.fingerprint
    assert lines[1]["event"] == "memory_stage_tls_certificate_expiring" and lines[1]["expired"] is False
    assert len(material.fingerprint) == 64


def test_no_warning_with_plenty_of_time_and_an_expired_certificate_is_flagged(capsys):
    cert_pem, key_pem = make_pair(days=90)
    good = se.load_tls_material({se.CERT_ENV: b64(cert_pem), se.KEY_ENV: b64(key_pem)})
    se.log_certificate_status(good)
    assert [json.loads(line)["level"] for line in capsys.readouterr().out.splitlines()] == ["info"]
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=120)
    expired = se.load_tls_material({se.CERT_ENV: b64(cert_pem), se.KEY_ENV: b64(key_pem)}, now=future)
    assert expired.days_to_expiry < 0
    se.log_certificate_status(expired)
    last = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert last["level"] == "warning" and last["expired"] is True


# ------------------------------------------------------------ files removed, TLS still served


def _tls_material():
    cert_pem, key_pem = make_pair()
    return cert_pem, se.load_tls_material({se.CERT_ENV: b64(cert_pem), se.KEY_ENV: b64(key_pem)})


def test_the_certificate_files_are_private_while_they_exist_and_gone_after_the_load(monkeypatch):
    seen = {}
    original_write = se.PrivateDir.write

    def spy(self, name, content):
        path = original_write(self, name, content)
        seen[name] = (path, stat.S_IMODE(path.stat().st_mode), stat.S_IMODE(path.parent.stat().st_mode))
        return path

    monkeypatch.setattr(se.PrivateDir, "write", spy)
    _, material = _tls_material()
    config = se.build_loaded_config(FastAPI(), material, "127.0.0.1", 0)
    assert config.loaded and config.ssl is not None
    assert set(seen) == {"server.crt", "server.key"}
    for path, mode, dir_mode in seen.values():
        assert mode == 0o600 and dir_mode == 0o700
        assert not path.exists() and not path.parent.exists()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_the_server_serves_tls_after_the_files_are_gone(monkeypatch):
    import uvicorn

    cert_pem, material = _tls_material()
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "ok"}

    directories = []
    original_init = se.PrivateDir.__init__

    def spy(self, prefix="x"):
        original_init(self, prefix)
        directories.append(self.path)

    monkeypatch.setattr(se.PrivateDir, "__init__", spy)
    port = _free_port()
    config = se.build_loaded_config(app, material, "127.0.0.1", port, log_level="warning")
    assert directories and not any(path.exists() for path in directories)  # gone BEFORE the server starts
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 10
        while not server.started and time.time() < deadline:
            time.sleep(0.05)
        assert server.started
        context = ssl.create_default_context(cadata=cert_pem)  # only this certificate is trusted
        import http.client

        connection = http.client.HTTPSConnection("localhost", port, context=context, timeout=5)
        connection.request("GET", "/health")
        response = connection.getresponse()
        assert response.status == 200 and json.loads(response.read()) == {"status": "ok"}
        assert connection.sock.version() in ("TLSv1.2", "TLSv1.3")
        connection.close()
        # a client that trusts nothing relevant is refused: this is real TLS, not plain HTTP
        with pytest.raises(ssl.SSLError):
            bad = http.client.HTTPSConnection("localhost", port, context=ssl.create_default_context(cadata=make_pair()[0]), timeout=5)
            bad.request("GET", "/health")
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_negative_control_without_the_early_load_the_files_would_be_needed_at_server_start():
    """Proves the ordering matters: deleting the files before ``load()`` breaks the server."""
    import uvicorn

    _, material = _tls_material()
    with se.PrivateDir("control-") as directory:
        config = uvicorn.Config(
            FastAPI(), host="127.0.0.1", port=_free_port(),
            ssl_certfile=str(directory.write("c.crt", material.cert_pem)),
            ssl_keyfile=str(directory.write("c.key", material.key_pem)),
        )
    assert not config.loaded  # files are removed, and nothing has read them
    with pytest.raises((FileNotFoundError, OSError)):
        uvicorn.Server(config).run()


# --------------------------------------------------------------------------- registry


def test_a_valid_registry_is_written_0600_in_a_private_dir_used_and_removed_at_exit(env):
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
    assert recorder.served == ("0.0.0.0", 8443, recorder.served[2])
    assert recorder.registry_mode == 0o600 and recorder.registry_dir_mode == 0o700
    assert not recorder.registry_path.exists() and not recorder.registry_path.parent.exists()
    assert recorder.ca_mode == 0o600 and not recorder.ca_path.exists()


def test_registry_in_raw_form_is_accepted(env):
    env[se.REGISTRY_ENV] = registry_text(2)
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "not base64 !!!", b64("this is not = valid [[ toml"), b64(""), b64("# only a comment\n"),
     b64('[[keys]]\nkid = "a"\nsub = "charlie"\npublic_key = "AAAA"\ncapabilities = []\n')],
)
def test_a_missing_malformed_or_empty_registry_stops_the_service_from_starting(env, value, capsys):
    if value is None:
        env.pop(se.REGISTRY_ENV)
    else:
        env[se.REGISTRY_ENV] = value
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
    assert recorder.served is None and recorder.factory_calls == []
    assert se.REGISTRY_ENV in capsys.readouterr().err


def test_the_unchanged_key_registry_refuses_an_unusable_file_through_the_real_factory(env, monkeypatch):
    """The real factory path: KeyRegistry.load_initial still raises, and the launcher maps it to a refusal."""
    from viharbot.memory_service import app as app_module

    def failing(environ, conninfo=None):
        raise ServiceAuthConfigError("service authentication is enabled but the key registry is missing, invalid or has no keys")

    monkeypatch.setattr(app_module, "create_app_from_environment", failing)
    recorder = Recorder()
    with pytest.raises(se.StageConfigError):
        se.run_serve(env, server=recorder.server)
    assert recorder.served is None


def test_the_real_factory_builds_an_app_that_requires_assertions(env, tmp_path):
    """No database is reachable; the service must still build and still refuse unauthenticated calls."""
    from fastapi.testclient import TestClient

    env = {**env, "MEMORY_DB_HOST": "127.0.0.1", "MEMORY_DB_PORT": str(_free_port()), se.DB_CA_ENV: ""}
    holder = {}

    def capture(app, material, host, port):
        holder["app"] = app

    assert se.main(env, server=capture) == 0
    client = TestClient(holder["app"])
    assert client.get("/memory/agent/charlie").status_code == 401  # verifier on, no off switch
    assert client.get("/health").status_code in (200, 503)


# ------------------------------------------------------------------------- database (item 4)

BASE = {"MEMORY_DB_HOST": "h", "MEMORY_DB_NAME": "n", "MEMORY_DB_USER": "u", "MEMORY_DB_PASSWORD": "p"}


def test_compose_style_conninfo_is_unchanged():
    assert conninfo_from_environment(BASE) == "host=h port=5432 dbname=n user=u password=p sslmode=prefer"
    assert conninfo_from_environment({**BASE, "MEMORY_DB_SSLMODE": "disable", "MEMORY_DB_PORT": "5433"}) == (
        "host=h port=5433 dbname=n user=u password=p sslmode=disable"
    )
    migrate = {k.replace("MEMORY_DB_", "MEMORY_MIGRATE_DB_"): v for k, v in BASE.items()}
    assert conninfo_from_environment(migrate, prefix="MEMORY_MIGRATE_DB_") == (
        "host=h port=5432 dbname=n user=u password=p sslmode=prefer"
    )


def test_serve_mode_conninfo_has_verify_full_and_the_root_certificate(env):
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
    conninfo = recorder.factory_calls[0][1]
    assert "sslmode=verify-full" in conninfo and "sslrootcert=" in conninfo
    assert "host=db.example.invalid" in conninfo and "port=25060" in conninfo and "user=memory_service" in conninfo


def test_serve_mode_forces_verify_full_over_a_weaker_configured_mode(env):
    env["MEMORY_DB_SSLMODE"] = "require"
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
    assert "sslmode=verify-full" in recorder.factory_calls[0][1]
    env.pop("MEMORY_DB_SSLMODE")
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
    assert "sslmode=verify-full" in recorder.factory_calls[1][1]


def test_strict_conninfo_function_directly():
    assert conninfo_from_environment(BASE | {"MEMORY_DB_HOST": "db.internal"}, sslrootcert="/x/ca.pem", strict_tls=True) == (
        "host=db.internal port=5432 dbname=n user=u password=p sslmode=verify-full sslrootcert=/x/ca.pem"
    )
    with pytest.raises(ServiceAuthConfigError):
        conninfo_from_environment(BASE, strict_tls=True)  # host "h" is not loopback, no CA


def test_serve_mode_refuses_a_remote_host_without_a_ca(env, capsys):
    env.pop(se.DB_CA_ENV)
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
    assert recorder.factory_calls == [] and recorder.served is None
    assert "verify-full" in capsys.readouterr().err


@pytest.mark.parametrize("ca", ["####", b64("not a certificate"), b64("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")])
def test_serve_mode_refuses_a_malformed_ca(env, ca):
    env[se.DB_CA_ENV] = ca
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_REFUSED
    assert recorder.factory_calls == []


def test_serve_mode_accepts_the_ca_as_base64_or_pem_and_a_loopback_host_without_one(env):
    ca_pem = make_pair()[0]
    for form in (b64(ca_pem), ca_pem, ca_pem.replace("\n", "\\n")):
        env[se.DB_CA_ENV] = form
        recorder = Recorder()
        assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
        assert "sslrootcert=" in recorder.factory_calls[0][1]
    for host in ("127.0.0.1", "localhost", "::1"):
        local = {**env, "MEMORY_DB_HOST": host, se.DB_CA_ENV: ""}
        recorder = Recorder()
        assert se.main(local, server=recorder.server, app_factory=recorder.app_factory) == 0
        assert "sslrootcert" not in recorder.factory_calls[0][1]


# ---------------------------------------------------------------------- logs, role, port


def test_no_secret_value_or_connection_string_reaches_the_logs(env, password, capsys, material):
    recorder = Recorder()
    assert se.main(env, server=recorder.server, app_factory=recorder.app_factory) == 0
    # and a set of refusals, which must be just as quiet
    for name in (se.CERT_ENV, se.KEY_ENV, se.REGISTRY_ENV, se.DB_CA_ENV):
        broken = {**env, name: "zz" + env[name][2:]}
        se.main(broken, server=recorder.server, app_factory=recorder.app_factory)
    captured = capsys.readouterr()
    text = captured.out + captured.err
    body = lambda pem: re.sub(r"-----[^-]+-----|\s", "", pem)  # noqa: E731
    assert password not in text and "password=" not in text and "sslrootcert" not in text
    assert "postgresql://" not in text and "host=" not in text
    for secret in (body(material[0])[:40], body(material[1])[:40], env[se.REGISTRY_ENV][:30], env[se.KEY_ENV][:30]):
        assert secret not in text
    assert "BEGIN" not in text
    for line in captured.out.splitlines():
        json.loads(line)  # every stdout line is one JSON object


def test_the_launcher_does_nothing_unless_the_role_is_serve(env, capsys):
    for role in (None, "", "bootstrap", "SERVE", "x"):
        local = {k: v for k, v in env.items() if k != se.ROLE_ENV}
        if role is not None:
            local[se.ROLE_ENV] = role
        recorder = Recorder()
        assert se.main(local, server=recorder.server, app_factory=recorder.app_factory) == se.EXIT_BAD_ROLE
        assert recorder.served is None and recorder.factory_calls == []


def test_the_port_defaults_to_8443_and_rejects_nonsense(env):
    assert se._port({}) == 8443 and se._port({"PORT": "9443"}) == 9443
    for bad in ("0", "70000", "abc", "-1", "80.5"):
        with pytest.raises(se.StageConfigError):
            se._port({"PORT": bad})


# --------------------------------------------------------------- Compose and the image (static)

ORIGINAL_COMPOSE_COMMAND = (
    'if [ -z \\"$MEMORY_DB_PASSWORD\\" ] || { [ -n \\"$MEMORY_TLS_CERT_FILE\\" ] && [ ! -r \\"$MEMORY_TLS_CERT_FILE\\" ]; }; '
    "then echo 'memory: development credentials or certificates are missing. Run python scripts/dev_service_auth.py "
    "on the host, then docker compose up -d' >&2; exit 1; fi; exec uvicorn "
    "viharbot.memory_service.app:create_app_from_environment --factory --host 0.0.0.0 --port \\\"$PORT\\\" "
    '${MEMORY_TLS_CERT_FILE:+--ssl-certfile \\"$MEMORY_TLS_CERT_FILE\\" --ssl-keyfile \\"$MEMORY_TLS_KEY_FILE\\"}"]'
)


def test_the_image_dispatches_on_the_role_and_keeps_the_compose_command_as_it_was():
    text = (ROOT / "Dockerfile.memory").read_text()
    cmd = [line for line in text.splitlines() if line.startswith("CMD ")]
    assert len(cmd) == 1
    prefix = 'CMD ["sh", "-c", "if [ -n \\"$MEMORY_STAGE_ROLE\\" ]; then exec python -m viharbot.memory_service.stage_entry; fi; '
    assert cmd[0] == prefix + ORIGINAL_COMPOSE_COMMAND  # the Compose branch is the old command, byte for byte
    assert (ROOT / "src" / "viharbot" / "memory_service" / "stage_entry.py").is_file()
    assert "USER 1001:1001" in text


def test_compose_does_not_select_the_launcher_and_keeps_its_file_based_tls_and_health_check():
    compose = (ROOT / "compose.yaml").read_text()
    assert "MEMORY_STAGE_ROLE" not in compose
    memory = re.search(r"^  memory:\n(.*?)(?=^  \S)", compose, re.S | re.M).group(1)
    assert "MEMORY_TLS_CERT_FILE: /run/memory/tls/memory.pem" in memory
    assert "VIHARBOT_KEY_REGISTRY: /run/memory/key-registry.toml" in memory
    assert "MEMORY_DB_HOST: postgres" in memory and "_PEM" not in memory and "MEMORY_DB_CA" not in memory
    assert "https://127.0.0.1:8000/health" in memory


def test_the_launcher_has_no_logging_of_values_in_source():
    source = (ROOT / "src" / "viharbot" / "memory_service" / "stage_entry.py").read_text()
    assert "logger." not in source
    assert not re.search(r"_log\([^)]*(conninfo|password|environ\[)", source)
    assert os.environ.get("MEMORY_STAGE_ROLE") != "serve"  # this test run is not itself in serve mode
