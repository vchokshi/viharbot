#!/usr/bin/env python3
"""Generate the probe's self-signed certificate. Run LOCALLY by the founder. THROWAWAY.

    python deploy/digitalocean-probe/gen_probe_cert.py --san probe-service --san probe-service.internal

Writes four files into a gitignored directory (default `data/probe-cert/`, which the repository's
`data/` rule ignores) and prints only the certificate's public facts (subject alternative names,
validity, SHA-256 fingerprint). It never prints the private key.

    probe.crt.pem   certificate (public)
    probe.key.pem   private key, mode 0600
    probe.crt.b64   certificate as one base64 line (paste-safe in a control panel)
    probe.key.b64   private key as one base64 line

Paste `probe.crt.b64` into PROBE_TLS_CERT_PEM and `probe.key.b64` into PROBE_TLS_KEY_PEM (encrypted
variables) of the probe app, and `probe.crt.b64` into PROBE_TLS_CA_PEM of the client component. The
probe accepts raw PEM, PEM with literal backslash-n, or base64. Nothing generated is committed:
the script refuses an output directory inside the repository unless it is under `data/`.

One certificate serves both ends: it is the server's certificate and the only certificate the
client trusts (verification stays on). EC P-256, valid for one year, key usage limited to a TLS
server. Needs only the `cryptography` package, already a ViharBot dependency.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import ipaddress
import os
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = REPO_ROOT / "data" / "probe-cert"
VALID_DAYS = 365
DEFAULT_SANS = ("probe-service",)


def parse_sans(values: list[str]) -> list[str]:
    """Flatten repeated and comma-separated --san values; drop blanks and duplicates, keep order."""
    seen: list[str] = []
    for value in values:
        for item in value.split(","):
            item = item.strip()
            if item and item not in seen:
                seen.append(item)
    if not seen:
        raise ValueError("at least one subject alternative name is required")
    return seen


def _general_name(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        if any(ch in value for ch in " /:@") or value.startswith("."):
            raise ValueError(f"not a valid DNS name or IP address: {value!r}") from None
        return x509.DNSName(value)


def generate_certificate(sans: list[str], *, days: int = VALID_DAYS,
                         now: datetime.datetime | None = None) -> tuple[bytes, bytes]:
    """Return (certificate PEM, private key PEM) for a self-signed server certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    start = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(minutes=5)
    first = sans[0]
    name = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ViharBot probe (throwaway)"),
        x509.NameAttribute(NameOID.COMMON_NAME, first[:64]),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([_general_name(s) for s in sans]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                          data_encipherment=False, key_agreement=False, key_cert_sign=False,
                          crl_sign=False, encipher_only=False, decipher_only=False),
            critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()),
                       critical=False)
        .sign(key, hashes.SHA256())
    )
    return (cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()))


def check_output_dir(output: Path, repo_root: Path = REPO_ROOT) -> Path:
    """Refuse a directory inside the repository unless it is under the gitignored `data/`."""
    resolved = output.resolve()
    try:
        relative = resolved.relative_to(repo_root.resolve())
    except ValueError:
        return resolved  # outside the repository: nothing can be committed from there
    if relative.parts[:1] != ("data",):
        raise ValueError(f"refusing to write inside the repository outside data/ ({relative}); "
                         "generated certificates and keys must never be committed")
    return resolved


def _write(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        os.fchmod(fd, mode)
        os.write(fd, data)
    finally:
        os.close(fd)


def write_files(output: Path, cert_pem: bytes, key_pem: bytes, *, force: bool = False) -> list[Path]:
    names = ("probe.crt.pem", "probe.key.pem", "probe.crt.b64", "probe.key.b64")
    existing = [output / n for n in names if (output / n).exists()]
    if existing and not force:
        raise FileExistsError("already exists: " + ", ".join(p.name for p in existing)
                              + " (use --force to replace)")
    output.mkdir(parents=True, exist_ok=True)
    os.chmod(output, 0o700)
    contents = {
        "probe.crt.pem": cert_pem, "probe.key.pem": key_pem,
        "probe.crt.b64": base64.b64encode(cert_pem) + b"\n",
        "probe.key.b64": base64.b64encode(key_pem) + b"\n",
    }
    paths = []
    for name, data in contents.items():
        _write(output / name, data, 0o600)
        paths.append(output / name)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--san", action="append", default=[],
                        help="subject alternative name (DNS name or IP); repeat or comma-separate. "
                             f"Default: {', '.join(DEFAULT_SANS)}")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                        help="output directory (default: data/probe-cert, gitignored)")
    parser.add_argument("--days", type=int, default=VALID_DAYS)
    parser.add_argument("--force", action="store_true", help="replace existing files")
    args = parser.parse_args(argv)
    try:
        sans = parse_sans(args.san or list(DEFAULT_SANS))
        output = check_output_dir(args.out)
        cert_pem, key_pem = generate_certificate(sans, days=args.days)
        paths = write_files(output, cert_pem, key_pem, force=args.force)
    except (ValueError, FileExistsError) as exc:
        print(f"gen_probe_cert: {exc}", file=sys.stderr)
        return 2
    cert = x509.load_pem_x509_certificate(cert_pem)
    print("Certificate written (the private key is never printed):")
    for path in paths:
        print(f"  {path}")
    print("subject alternative names:", ", ".join(sans))
    print("valid until:", cert.not_valid_after_utc.isoformat(timespec="seconds"))
    print("sha256 fingerprint:", hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest())
    print("Paste the .b64 files into encrypted variables in the control panel. Do not commit, "
          "email or paste them anywhere else. Delete the directory at teardown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
