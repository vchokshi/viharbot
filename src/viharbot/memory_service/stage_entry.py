"""Stage launcher for the memory service (docs/stage-memory-design.md, work items 1, 2 and 4).

Selected by ``MEMORY_STAGE_ROLE=serve``. With the variable unset nothing here runs: the image's
default command is the Compose command, byte for byte (``Dockerfile.memory``). Compose keeps its
file-based TLS (``MEMORY_TLS_CERT_FILE`` and friends), its mounted key registry and its
unencrypted private-network database connection; none of that changes.

Why a launcher: App Platform gives the service encrypted variables, not files. This module turns
three variables into what the existing machinery already takes, without a new dependency and
without changing the A2 verifier:

* ``MEMORY_TLS_CERT_PEM`` and ``MEMORY_TLS_KEY_PEM`` (base64 of PEM, raw PEM, PEM with literal
  backslash-n, or newline-flattened PEM). Both must parse and the key must match the certificate.
  They are written 0600 into a 0700 temporary directory, loaded by uvicorn, and deleted before the
  server accepts a connection.
* ``VIHARBOT_KEY_REGISTRY_B64`` (base64 of the registry TOML; raw TOML is also accepted). It is
  written 0600 into a private directory and handed to the unchanged ``KeyRegistry``, which refuses
  to start on a missing, malformed or empty registry. That file is NOT deleted after the first
  load: ``KeyRegistry`` re-reads its path every minute and fails closed (503) when it cannot, so the
  file lives as long as the process and is removed at exit. It holds public keys only.
* ``MEMORY_DB_CA_PEM`` (base64 or PEM; one or more certificates). libpq reads ``sslrootcert`` for
  every new connection (the service opens one per operation), so this file also lives as long as
  the process and is removed at exit. The connection is forced to ``sslmode=verify-full``.

Ordering with uvicorn (tested, ``tests/test_memory_stage_entry.py``): ``uvicorn.Config.load()``
builds the ``ssl.SSLContext`` from the certificate and key paths; ``Server`` skips ``load()`` when
the config is already loaded. So the launcher calls ``config.load()``, deletes the files, then runs
the server. A negative control in the tests shows that without the early ``load()`` the files
would be read at server start, which is why the order matters.

No value, path or connection string is ever logged or printed. Refusals are fixed messages that
name a variable at most. The log lines are one JSON object each on stdout.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

ROLE_ENV = "MEMORY_STAGE_ROLE"
SERVE_ROLE = "serve"
DEFAULT_PORT = 8443  # design 2.3; the image sets PORT=8000, so the Stage spec must set PORT=8443
EXPIRY_WARNING_DAYS = 30

CERT_ENV = "MEMORY_TLS_CERT_PEM"
KEY_ENV = "MEMORY_TLS_KEY_PEM"
REGISTRY_ENV = "VIHARBOT_KEY_REGISTRY_B64"
DB_CA_ENV = "MEMORY_DB_CA_PEM"

EXIT_REFUSED = 1
EXIT_BAD_ROLE = 2


class StageConfigError(Exception):
    """A refusal to start. The message is fixed text and never contains a value."""


# ----------------------------------------------------------------------------- logging


def _log(level: str, event: str, **fields: Any) -> None:
    """One content-free JSON line on stdout. Callers pass only numbers, names and fingerprints."""
    line = {"timestamp": datetime.now(timezone.utc).isoformat(), "level": level, "agent": "memory",
            "event": event, **fields}
    print(json.dumps(line, sort_keys=True, default=str), flush=True)


# ------------------------------------------------------------------------ PEM from a variable

_PEM_BLOCK = re.compile(r"-----BEGIN ([A-Z0-9 ]+)-----\s*(.*?)\s*-----END \1-----", re.S)


def decode_pem_variable(value: str | None, name: str) -> list[tuple[str, str]]:
    """Return every PEM block in a variable as ``(label, normalized pem)``.

    Accepts base64 of PEM, raw PEM, PEM with literal backslash-n sequences, and PEM whose newlines
    were flattened to spaces. Raises ``StageConfigError`` naming only the variable.
    """
    if value is None or not value.strip():
        raise StageConfigError(f"{name} is not set or is empty")
    text = value.strip()
    if "-----BEGIN" not in text:
        try:
            text = base64.b64decode(text, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise StageConfigError(f"{name} is neither PEM text nor base64 of PEM") from None
    text = text.replace("\\n", "\n")
    blocks = []
    for match in _PEM_BLOCK.finditer(text):
        body = re.sub(r"\s+", "", match.group(2))
        if not body:
            continue
        wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
        blocks.append((match.group(1), f"-----BEGIN {match.group(1)}-----\n{wrapped}\n-----END {match.group(1)}-----\n"))
    if not blocks:
        raise StageConfigError(f"{name} does not contain a PEM block")
    return blocks


def _certificates(blocks: list[tuple[str, str]], name: str) -> list[Any]:
    from cryptography import x509

    certs = [pem for label, pem in blocks if label == "CERTIFICATE"]
    if not certs:
        raise StageConfigError(f"{name} contains no certificate")
    try:
        return [x509.load_pem_x509_certificate(pem.encode("ascii")) for pem in certs]
    except ValueError:
        raise StageConfigError(f"{name} does not parse as a certificate") from None


def _public_der(public_key: Any) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


class TlsMaterial:
    """A validated certificate chain and private key, as normalized PEM text."""

    def __init__(self, cert_pem: str, key_pem: str, days_to_expiry: int, fingerprint: str) -> None:
        self.cert_pem, self.key_pem = cert_pem, key_pem
        self.days_to_expiry, self.fingerprint = days_to_expiry, fingerprint


def load_tls_material(environ: Mapping[str, str], now: datetime | None = None) -> TlsMaterial:
    """Decode and validate the certificate and key variables. Raises ``StageConfigError``."""
    from cryptography.hazmat.primitives import hashes, serialization

    cert_blocks = decode_pem_variable(environ.get(CERT_ENV), CERT_ENV)
    certs = _certificates(cert_blocks, CERT_ENV)
    key_blocks = [pem for label, pem in decode_pem_variable(environ.get(KEY_ENV), KEY_ENV) if label.endswith("PRIVATE KEY")]
    if not key_blocks:
        raise StageConfigError(f"{KEY_ENV} contains no private key")
    try:
        key = serialization.load_pem_private_key(key_blocks[0].encode("ascii"), password=None)
    except (ValueError, TypeError):  # malformed, or encrypted (no passphrase is accepted)
        raise StageConfigError(f"{KEY_ENV} does not parse as an unencrypted private key") from None
    leaf = certs[0]
    if _public_der(leaf.public_key()) != _public_der(key.public_key()):
        raise StageConfigError(f"{KEY_ENV} does not match {CERT_ENV}")
    current = now or datetime.now(timezone.utc)
    remaining = leaf.not_valid_after_utc - current
    days = remaining.days  # floor: an expired certificate is negative
    return TlsMaterial(
        cert_pem="".join(pem for label, pem in cert_blocks if label == "CERTIFICATE"),
        key_pem=key_blocks[0],
        days_to_expiry=days,
        fingerprint=leaf.fingerprint(hashes.SHA256()).hex(),
    )


def log_certificate_status(material: TlsMaterial) -> None:
    """One line at start; a warning line (as well) when fewer than 30 days remain."""
    _log("info", "memory_stage_tls_certificate", days_to_expiry=material.days_to_expiry,
         fingerprint_sha256=material.fingerprint)
    if material.days_to_expiry < EXPIRY_WARNING_DAYS:
        _log("warning", "memory_stage_tls_certificate_expiring", days_to_expiry=material.days_to_expiry,
             expired=material.days_to_expiry < 0, fingerprint_sha256=material.fingerprint)


# ------------------------------------------------------------------------------ temp files


class PrivateDir:
    """A 0700 temporary directory whose files are 0600 (created O_EXCL, then fchmod, so the umask
    cannot loosen them). ``remove()`` is idempotent and also runs from ``__exit__``."""

    def __init__(self, prefix: str = "memory-stage-") -> None:
        self.path = Path(tempfile.mkdtemp(prefix=prefix))
        os.chmod(self.path, 0o700)

    def write(self, name: str, content: str) -> Path:
        target = self.path / name
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        return target

    def remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> "PrivateDir":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.remove()


# ----------------------------------------------------------------------------- registry, CA


def decode_registry_variable(value: str | None) -> str:
    """The registry TOML text from ``VIHARBOT_KEY_REGISTRY_B64`` (base64; raw TOML also accepted)."""
    if value is None or not value.strip():
        raise StageConfigError(f"{REGISTRY_ENV} is not set or is empty")
    text = value.strip()
    if "[[keys]]" not in text:
        try:
            text = base64.b64decode(text, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise StageConfigError(f"{REGISTRY_ENV} is neither base64 nor registry text") from None
    return text if text.endswith("\n") else text + "\n"


def check_registry(text: str) -> None:
    """Refuse a malformed or empty registry early, with the unchanged parser ``KeyRegistry`` uses."""
    from ..service_auth import parse_registry

    try:
        keys = parse_registry(text)
    except (ValueError, TypeError, AttributeError, RecursionError):
        raise StageConfigError(f"{REGISTRY_ENV} is not a valid key registry") from None
    if not keys:
        raise StageConfigError(f"{REGISTRY_ENV} has no keys")


def load_db_ca(environ: Mapping[str, str]) -> str | None:
    """The cluster CA as normalized PEM, or None when the variable is unset. Malformed raises."""
    if not (environ.get(DB_CA_ENV) or "").strip():
        return None
    blocks = decode_pem_variable(environ.get(DB_CA_ENV), DB_CA_ENV)
    _certificates(blocks, DB_CA_ENV)
    return "".join(pem for label, pem in blocks if label == "CERTIFICATE")


# ------------------------------------------------------------------------------- TLS server


def build_loaded_config(app: Any, material: TlsMaterial, host: str, port: int, **config_kwargs: Any) -> Any:
    """A ``uvicorn.Config`` whose SSL context is already built and whose files are already gone.

    ``config.load()`` creates the ``ssl.SSLContext`` from the paths; ``uvicorn.Server`` does not
    load a config that is loaded, so deleting the files after this call is safe.
    """
    import ssl

    import uvicorn

    with PrivateDir("memory-tls-") as directory:
        cert_path = directory.write("server.crt", material.cert_pem)
        key_path = directory.write("server.key", material.key_pem)
        config = uvicorn.Config(app, host=host, port=port, ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
                                **config_kwargs)
        config.load()
    if config.ssl is not None:
        config.ssl.minimum_version = ssl.TLSVersion.TLSv1_2
    return config


def _run_server(app: Any, material: TlsMaterial, host: str, port: int) -> None:
    import uvicorn

    uvicorn.Server(build_loaded_config(app, material, host, port)).run()


# --------------------------------------------------------------------------------- entry


def _port(environ: Mapping[str, str]) -> int:
    text = (environ.get("PORT") or "").strip() or str(DEFAULT_PORT)
    if not (text.isascii() and text.isdigit() and 1 <= int(text) <= 65535):
        raise StageConfigError("PORT is not a valid port number")
    return int(text)


def _default_app_factory(env: Mapping[str, str], conninfo: str) -> Any:
    from .app import create_app_from_environment

    return create_app_from_environment(env, conninfo=conninfo)


def run_serve(
    environ: Mapping[str, str],
    *,
    server: Callable[[Any, TlsMaterial, str, int], None] = _run_server,
    app_factory: Callable[[Mapping[str, str], str], Any] = _default_app_factory,
) -> int:
    """Validate everything, then serve. Every refusal is a ``StageConfigError`` before any listener."""
    from ..service_auth import ServiceAuthConfigError
    from .app import conninfo_from_environment

    material = load_tls_material(environ)
    registry_text = decode_registry_variable(environ.get(REGISTRY_ENV))
    check_registry(registry_text)
    ca_pem = load_db_ca(environ)
    port = _port(environ)

    with PrivateDir("memory-stage-") as long_lived:  # registry and CA live as long as the process
        registry_path = long_lived.write("key-registry.toml", registry_text)
        ca_path = long_lived.write("db-ca.pem", ca_pem) if ca_pem else None
        try:
            conninfo = conninfo_from_environment(
                environ, sslrootcert=str(ca_path) if ca_path else None, strict_tls=True
            )
        except ServiceAuthConfigError as error:  # fixed text naming a variable, never a value
            raise StageConfigError(str(error)) from None
        except Exception:
            raise StageConfigError("the database connection settings are not valid") from None
        app_env = {**environ, "VIHARBOT_KEY_REGISTRY": str(registry_path)}
        try:
            app = app_factory(app_env, conninfo)
        except ServiceAuthConfigError:
            raise StageConfigError(f"{REGISTRY_ENV} is not a usable key registry") from None
        log_certificate_status(material)
        _log("info", "memory_stage_start", port=port, database_tls="verify-full")
        server(app, material, "0.0.0.0", port)
    return 0


def main(environ: Mapping[str, str] | None = None, **kwargs: Any) -> int:
    env = os.environ if environ is None else environ
    role = (env.get(ROLE_ENV) or "").strip()
    if role != SERVE_ROLE:
        print(f"memory-stage: {ROLE_ENV} must be '{SERVE_ROLE}' to use this launcher", file=sys.stderr)
        return EXIT_BAD_ROLE
    try:
        return run_serve(env, **kwargs)
    except StageConfigError as error:
        print(f"memory-stage: refusing to start: {error}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
