#!/usr/bin/env python3
"""Generate the Stage memory materials. Run ONLY on the founder's own machine.

    python scripts/stage_memory_materials.py                   # everything, first time
    python scripts/stage_memory_materials.py --passwords-only  # new window: fresh role passwords only
    python scripts/stage_memory_materials.py --force           # replace certificate, key and registry
    python scripts/stage_memory_materials.py --force --rotate-passwords   # replace passwords too

Writes into a gitignored directory under data/ (default data/stage-memory/, mode 0700; every file is
mode 0600) and nothing anywhere else: an output path outside data/, or inside the repository outside
data/, is refused. It prints only facts that are not secret: the certificate's SHA-256 fingerprint
and expiry, the file names written, and, for each file, the environment variable name it is meant
for. It never prints a secret, logs one, or takes one on the command line. Nothing is committed,
and no agent should run it or read its output files (docs/stage-memory-design.md, decision N2).

Files, and the variable each is pasted into (as an encrypted variable) in the control panel:

  memory.crt.b64            VIHARBOT_MEMORY_CA_PEM      (component `synthesis`: Charlie's ONLY trusted
                                                         certificate)
                            MEMORY_TLS_CERT_PEM         (component `memory`: the server certificate)
  memory.key.b64            MEMORY_TLS_KEY_PEM          (component `memory`: the server private key)
  charlie-signing.key       VIHARBOT_SIGNING_KEY        (component `synthesis`: Charlie's Ed25519 seed,
                                                         base64url). Set VIHARBOT_SIGNING_KID to the
                                                         key id this script prints.
  memory-registry.b64       VIHARBOT_KEY_REGISTRY_B64   (component `memory`: base64 of the registry)
  memory-migrator-password.txt  MEMORY_DB_MIGRATOR_PASSWORD   (the migrator role; bootstrap job)
  memory-service-password.txt   MEMORY_DB_SERVICE_PASSWORD    (the service role; bootstrap job)
                                MEMORY_DB_PASSWORD            (component `memory`: the same value)

Also written, for your password manager and for checking (not variables): memory.crt.pem,
memory.key.pem (the private key), charlie-signing.pub (Charlie's public key, base64url) and
memory-registry.toml (the registry as text). The registry holds public keys only: subject `charlie`,
capabilities memory.read and memory.write, nothing else.

Overwriting: existing material is never replaced without --force. --force replaces the certificate,
signing key and registry but NOT the two role passwords (a live database keeps the passwords it was
created with, and new files would silently diverge from it); add --rotate-passwords to replace them
too. --passwords-only regenerates just the two role passwords, which is what each new window needs,
and leaves everything else untouched. One certificate, EC P-256, one name (`memory`) unless you add
more with --san, 365 days unless you pass --days. Needs only the `cryptography` package.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import ipaddress
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID  # noqa: E402

from viharbot import service_auth  # noqa: E402

DATA_ROOT = REPO_ROOT / "data"
DEFAULT_OUTPUT = DATA_ROOT / "stage-memory"
DEFAULT_NAME = "memory"
DEFAULT_DAYS = 365
MAX_DAYS = 3650
SUBJECT = "charlie"
DEFAULT_KID = "charlie-stage-1"
CAPABILITIES = (service_auth.MEMORY_READ_CAPABILITY, service_auth.MEMORY_WRITE_CAPABILITY)

CERT_FILES = ("memory.crt.pem", "memory.key.pem", "memory.crt.b64", "memory.key.b64")
KEY_FILES = ("charlie-signing.key", "charlie-signing.pub")
REGISTRY_FILES = ("memory-registry.toml", "memory-registry.b64")
PASSWORD_FILES = ("memory-migrator-password.txt", "memory-service-password.txt")
MATERIAL_FILES = CERT_FILES + KEY_FILES + REGISTRY_FILES

# File name -> the environment variable name(s) it is intended for. Names only, never values.
VARIABLES: dict[str, tuple[str, ...]] = {
    "memory.crt.b64": ("VIHARBOT_MEMORY_CA_PEM", "MEMORY_TLS_CERT_PEM"),
    "memory.key.b64": ("MEMORY_TLS_KEY_PEM",),
    "charlie-signing.key": ("VIHARBOT_SIGNING_KEY",),
    "memory-registry.b64": ("VIHARBOT_KEY_REGISTRY_B64",),
    "memory-migrator-password.txt": ("MEMORY_DB_MIGRATOR_PASSWORD",),
    "memory-service-password.txt": ("MEMORY_DB_SERVICE_PASSWORD", "MEMORY_DB_PASSWORD"),
    "memory.crt.pem": (),
    "memory.key.pem": (),
    "charlie-signing.pub": (),
    "memory-registry.toml": (),
}


def check_output_dir(output: Path, data_root: Path = DATA_ROOT) -> Path:
    """The resolved output directory, which must be a subdirectory of ``data_root`` (gitignored).

    Anything else is refused: outside the repository (it would escape the gitignore guarantee and
    this tool's contract), inside it outside data/, or data/ itself. Symlinks are resolved first.
    """
    resolved = output.resolve()
    root = data_root.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError(f"refusing to write outside {data_root.name}/ ({output}); "
                         "generated secrets must stay in the gitignored data/ directory")
    return resolved


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def parse_names(primary: str, extra: list[str]) -> list[str]:
    """The certificate names: the primary first, then extras; blanks and duplicates dropped."""
    names: list[str] = []
    for value in [primary, *[item for chunk in extra for item in chunk.split(",")]]:
        value = value.strip()
        if value and value not in names:
            names.append(value)
    if not names:
        raise ValueError("at least one certificate name is required")
    return names


def _general_name(value: str) -> x509.GeneralName:
    try:
        return x509.IPAddress(ipaddress.ip_address(value))
    except ValueError:
        if any(ch in value for ch in " /:@*") or value.startswith("."):
            raise ValueError(f"not a valid DNS name or IP address: {value!r}") from None
        return x509.DNSName(value)


def generate_certificate(names: list[str], *, days: int = DEFAULT_DAYS,
                         now: datetime.datetime | None = None) -> tuple[bytes, bytes]:
    """(certificate PEM, private key PEM): self-signed EC P-256 server certificate, not a CA."""
    if not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--days must be between 1 and {MAX_DAYS}")
    key = ec.generate_private_key(ec.SECP256R1())
    start = (now or datetime.datetime.now(datetime.timezone.utc)) - datetime.timedelta(minutes=5)
    subject = x509.Name([
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "ViharBot Stage memory"),
        x509.NameAttribute(NameOID.COMMON_NAME, names[0][:64]),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start).not_valid_after(start + datetime.timedelta(days=days))
        .add_extension(x509.SubjectAlternativeName([_general_name(n) for n in names]), critical=False)
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


def generate_signing_key() -> tuple[str, str]:
    """(private seed, public key), both base64url. The seed is what the service's loader accepts."""
    private = Ed25519PrivateKey.generate()
    seed = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64url(seed), _b64url(public)


def build_registry(public_key: str, kid: str = DEFAULT_KID) -> str:
    """The memory service's registry text. Verified through ``service_auth.parse_registry``."""
    if not kid or any(ch in kid for ch in '"\\\n\r'):
        raise ValueError("the key id must be non-empty and contain no quote, backslash or newline")
    listed = ", ".join(f'"{name}"' for name in CAPABILITIES)
    text = (
        "# Memory service registry generated by scripts/stage_memory_materials.py. Public keys only.\n"
        "[[keys]]\n"
        f'kid = "{kid}"\n'
        f'sub = "{SUBJECT}"\n'
        f'public_key = "{public_key}"\n'
        f"capabilities = [{listed}]\n"
    )
    entries = service_auth.parse_registry(text)  # a registry that does not parse is never written
    entry = entries.get(kid)
    if len(entries) != 1 or entry is None or entry.sub != SUBJECT or entry.capabilities != frozenset(CAPABILITIES):
        raise ValueError("the generated registry did not round-trip as expected")
    return text


def new_password() -> str:
    """32 characters of URL-safe randomness (A-Z a-z 0-9 - _): safe in a conninfo and a SQL literal."""
    return secrets.token_urlsafe(24)


def _write_new(path: Path, data: bytes) -> None:
    """Create ``path`` (replacing any file) atomically, with mode 0600 from the first byte."""
    temp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
    except BaseException:
        os.close(fd)
        temp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.replace(temp, path)


def plan(output: Path, *, passwords_only: bool, force: bool, rotate_passwords: bool) -> tuple[bool, bool]:
    """Decide (write_material, write_passwords), or raise FileExistsError when overwriting is refused."""
    existing_material = [n for n in MATERIAL_FILES if (output / n).exists()]
    existing_passwords = [n for n in PASSWORD_FILES if (output / n).exists()]
    if passwords_only:
        return False, True  # explicit intent: new passwords for a new window
    if (existing_material or existing_passwords) and not force:
        raise FileExistsError(
            "already exists: " + ", ".join(existing_material + existing_passwords)
            + " (use --force to replace the certificate, signing key and registry; "
              "--passwords-only for new role passwords only)")
    keep_passwords = len(existing_passwords) == len(PASSWORD_FILES) and not rotate_passwords
    return True, not keep_passwords


def generate(output: Path, *, names: list[str], days: int = DEFAULT_DAYS, kid: str = DEFAULT_KID,
             passwords_only: bool = False, force: bool = False, rotate_passwords: bool = False,
             data_root: Path = DATA_ROOT) -> dict[str, object]:
    """Write the requested material; return the non-secret facts to report. Nothing secret is returned."""
    output = check_output_dir(output, data_root)
    write_material, write_passwords = plan(
        output, passwords_only=passwords_only, force=force, rotate_passwords=rotate_passwords)
    contents: dict[str, bytes] = {}
    facts: dict[str, object] = {"kid": kid, "names": names}
    if write_material:
        cert_pem, key_pem = generate_certificate(names, days=days)
        cert = x509.load_pem_x509_certificate(cert_pem)
        seed, public = generate_signing_key()
        registry = build_registry(public, kid)
        contents.update({
            "memory.crt.pem": cert_pem, "memory.key.pem": key_pem,
            "memory.crt.b64": base64.b64encode(cert_pem) + b"\n",
            "memory.key.b64": base64.b64encode(key_pem) + b"\n",
            "charlie-signing.key": (seed + "\n").encode("ascii"),
            "charlie-signing.pub": (public + "\n").encode("ascii"),
            "memory-registry.toml": registry.encode("utf-8"),
            "memory-registry.b64": base64.b64encode(registry.encode("utf-8")) + b"\n",
        })
        facts["fingerprint"] = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
        facts["expires"] = cert.not_valid_after_utc.isoformat(timespec="seconds")
    if write_passwords:
        migrator, service = new_password(), new_password()
        while service == migrator:  # astronomically unlikely, and cheap to rule out
            service = new_password()
        contents["memory-migrator-password.txt"] = (migrator + "\n").encode("ascii")
        contents["memory-service-password.txt"] = (service + "\n").encode("ascii")
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output, 0o700)
    for name, data in contents.items():
        _write_new(output / name, data)
    facts["written"] = list(contents)
    facts["kept"] = [n for n in PASSWORD_FILES if n not in contents and (output / n).exists()]
    return facts


def main(argv: list[str] | None = None, *, data_root: Path = DATA_ROOT) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUTPUT,
                        help="output directory, which must be under data/ (default: data/stage-memory)")
    parser.add_argument("--name", default=DEFAULT_NAME,
                        help=f"the certificate's name (default: {DEFAULT_NAME}, the component name)")
    parser.add_argument("--san", action="append", default=[],
                        help="an extra certificate name (DNS name or IP); repeat or comma-separate. "
                             "Every name widens what the certificate is trusted for.")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"certificate validity in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--kid", default=DEFAULT_KID,
                        help=f"Charlie's key id in the registry (default: {DEFAULT_KID}); set "
                             "VIHARBOT_SIGNING_KID to the same value")
    parser.add_argument("--passwords-only", action="store_true",
                        help="regenerate only the two database role passwords (each new window)")
    parser.add_argument("--force", action="store_true",
                        help="replace existing certificate, signing key and registry; never the passwords "
                             "unless --rotate-passwords is also given")
    parser.add_argument("--rotate-passwords", action="store_true",
                        help="with --force, also replace the two role passwords")
    args = parser.parse_args(argv)
    try:
        facts = generate(
            args.out, names=parse_names(args.name, args.san), days=args.days, kid=args.kid,
            passwords_only=args.passwords_only, force=args.force, rotate_passwords=args.rotate_passwords,
            data_root=data_root)
    except (ValueError, FileExistsError) as error:
        print(f"stage_memory_materials: {error}", file=sys.stderr)
        return 2
    out = check_output_dir(args.out, data_root)
    print(f"Written to {out} (directory 0700, files 0600). No value is printed.")
    if "fingerprint" in facts:
        print("certificate names:", ", ".join(facts["names"]))  # type: ignore[arg-type]
        print("certificate expires:", facts["expires"])
        print("certificate sha256 fingerprint:", facts["fingerprint"])
        print("signing key id (VIHARBOT_SIGNING_KID):", facts["kid"])
    for name in facts["written"]:  # type: ignore[attr-defined]
        variables = VARIABLES.get(name, ())
        print(f"  {name}: " + (("paste into " + ", ".join(variables)) if variables else "keep for reference only"))
    for name in facts["kept"]:  # type: ignore[attr-defined]
        print(f"  {name}: kept unchanged (pass --rotate-passwords with --force to replace)")
    print("Store these in your password manager. Do not commit, email, chat or paste them anywhere "
          "else, and delete the directory at teardown.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
