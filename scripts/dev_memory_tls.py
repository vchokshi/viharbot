#!/usr/bin/env python3
"""Generate a throwaway DEVELOPMENT certificate authority and memory-service server certificate.

ADR-009 requires authenticated TLS for every non-loopback connection to the memory service, private
networks included. For local Docker Compose this script creates what that needs: a short-lived CA
and a server certificate for the hostname `memory` (also `localhost` and 127.0.0.1). The memory
service serves HTTPS with the server certificate; Charlie's client verifies it against the CA.

    python scripts/dev_service_auth.py          # the one setup command; it calls this script too
    python scripts/dev_memory_tls.py            # or just the TLS half, if the files are not there yet
    python scripts/dev_memory_tls.py --force    # replace them with a fresh CA and certificate

Written under the output directory (default data/dev-auth/tls, gitignored, never baked into an image):

    ca/ca.pem            the CA certificate (public). Mounted into the synthesis service only.
    server/memory.pem    the server certificate (public). Mounted into the memory service.
    server/memory.key    the server private key. Mounted into the memory service ONLY.

The CA private key is never written to disk: it signs the one server certificate and is discarded,
so nothing can mint another certificate under this CA later. Re-run with --force to rotate.

THESE ARE DEVELOPMENT CERTIFICATES for a loopback-only stack. They prove the client verifies TLS; they
are not a production or Stage certificate design. Stage and Production TLS (issuance, rotation,
revocation, where the key lives) are not done. The key file is mode 0644 on purpose so the container
user (uid 1001) can read a bind-mounted file owned by you; do not copy this anywhere else.

Only the `cryptography` package is needed (already a dependency of the project).
"""

from __future__ import annotations

import argparse
import datetime
import ipaddress
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEFAULT_OUTPUT = Path("data") / "dev-auth" / "tls"
VALID_DAYS = 30
SERVER_NAMES = ("memory", "localhost")


def _name(common_name: str) -> x509.Name:
    return x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ViharBot development"),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])


def generate(output: Path, *, force: bool = False) -> list[Path]:
    """Write the CA certificate, server certificate and server key; return the paths written."""
    ca_pem, server_pem, server_key = output / "ca" / "ca.pem", output / "server" / "memory.pem", output / "server" / "memory.key"
    targets = [ca_pem, server_pem, server_key]
    existing = [path for path in targets if path.exists()]
    if existing and not force:
        raise FileExistsError(
            "already exists: " + ", ".join(str(p) for p in existing) + " (use --force to replace)"
        )
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    expires = now + datetime.timedelta(days=VALID_DAYS)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = _name("ViharBot development CA (throwaway)")
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )

    key = ec.generate_private_key(ec.SECP256R1())
    sans: list[x509.GeneralName] = [x509.DNSName(n) for n in SERVER_NAMES]
    sans.append(x509.IPAddress(ipaddress.ip_address("127.0.0.1")))
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("memory")).issuer_name(ca_name).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(expires)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, key_cert_sign=False, crl_sign=False, content_commitment=False,
                          key_encipherment=False, data_encipherment=False, key_agreement=False,
                          encipher_only=False, decipher_only=False),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem.write_bytes(ca_cert.public_bytes(serialization.Encoding.PEM))
    server_pem.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    for path in targets:
        path.chmod(0o644)
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="default: data/dev-auth/tls")
    parser.add_argument("--force", action="store_true", help="replace the existing CA and certificate")
    args = parser.parse_args(argv)
    try:
        written = generate(args.output, force=args.force)
    except FileExistsError as error:
        print(f"dev_memory_tls: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    print(f"Development certificates only (valid {VALID_DAYS} days). Not for Stage or production.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
