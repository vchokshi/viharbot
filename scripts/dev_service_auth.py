#!/usr/bin/env python3
"""Generate development-only service-authentication keys and a registry for local Docker Compose.

ADR-006 step A2, issue #235. Local Compose turns service authentication on by default; this script
creates the keys it needs. Nothing it writes is committed or baked into an image: the default
output directory (data/dev-auth) is gitignored, and Compose mounts the files at run time.

    python scripts/dev_service_auth.py            # create the files if they are not there yet
    python scripts/dev_service_auth.py --force    # replace them with fresh keys

Written under the output directory:

    registry.toml        public keys only; mounted read-only into every service as the registry
    keys/web.key         the web service's signing seed (base64url, 32 bytes)
    keys/research.key    the Research service's own signing seed (it calls Security)
    keys/orchestrator.key  the optional `demo` profile orchestrator's own signing seed
    memory-client/charlie.key  Charlie's (the synthesis service's) own seed for calling the
                         memory service, in its own directory so Compose mounts only that file's
                         directory into the synthesis container (ADR-009, issue #40 PR 2)
    memory-registry.toml the MEMORY SERVICE's registry: public keys only, one key, subject
                         `charlie`, capabilities memory.read and memory.write and nothing else.
                         The web key is not in it, so the web service cannot reach memory.
    memory-db/postgres.env  throwaway bootstrap and role passwords (read by the postgres container)
    memory-db/migrate.env   the migration role's password (read by memory-migrate only)
    memory-db/service.env   the service role's password (read by the memory service only)
    tls/ca/ca.pem, tls/server/memory.{pem,key}  a throwaway development CA certificate and the
                         memory service's server certificate (scripts/dev_memory_tls.py; the CA key
                         is discarded). ca.pem goes to the synthesis service, the server key only
                         to the memory service.

This is the ONE setup command for the default local stack (service-auth keys, memory service
registry and key, TLS, and database passwords). Compose reads the passwords from memory-db/*.env and
fails closed, with a message pointing here, if they are missing.

Each service mounts only its own private key, and the registry binds each key to exactly one
subject, so Research cannot sign as `web`. Private key files are mode 0644 on purpose: the web
container runs as an unprivileged user (uid 1001) that must read a bind-mounted file owned by you.
They are development keys for a loopback-only stack. Never reuse them anywhere else, and do not
use this script for Stage or production (key storage and distribution there are open questions in
ADR-006).

Only the `cryptography` package is needed (already a dependency of the project).
"""

from __future__ import annotations

import argparse
import base64
import secrets
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev_memory_tls  # noqa: E402  (a sibling script; the TLS half of this one command)

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

DEFAULT_OUTPUT = Path("data") / "dev-auth"

# Capabilities are route families (docs/service-authentication.md). Authentication scopes only.
# They are kept as literals here so the script runs without importing the application.
_ALL_ROUTES = [
    "specialist.process",
    "research.run",
    "security.assess",
    "sage.process",
    "pixel.process",
    "pixel.read_deck",
]
IDENTITIES: tuple[tuple[str, str, list[str]], ...] = (
    # (subject, key file stem, capabilities the registry lets that subject assert)
    ("web", "web", _ALL_ROUTES),
    ("research", "research", ["security.assess"]),
    ("orchestrator", "orchestrator", ["specialist.process", "research.run", "security.assess"]),
)
KID_SUFFIX = "dev-1"
# The memory service verifies with its own registry (least privilege): only Charlie, only the two
# memory capabilities. Literals, like _ALL_ROUTES, so the script needs no application import.
MEMORY_IDENTITY = ("charlie", ["memory.read", "memory.write"])
MEMORY_KEY_DIR = "memory-client"
MEMORY_REGISTRY = "memory-registry.toml"
MEMORY_DB_DIR = "memory-db"
TLS_DIR = "tls"
BOOTSTRAP_USER = "viharbot_admin"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _write_secret_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{name}={value}\n" for name, value in values.items()), encoding="ascii")
    path.chmod(0o600)  # read on the host by Compose, never mounted into a container


def generate_memory(output: Path, *, force: bool = False) -> list[Path]:
    """Write everything the memory part of the default stack needs; return the paths written.

    Each group is skipped when it already exists and ``force`` is not set, so a running stack keeps
    working and an older checkout can add just what it lacks: Charlie's memory-client key with the
    memory service's registry (public key only), the throwaway database passwords, and the
    development TLS material.
    """
    written: list[Path] = []
    subject, capabilities = MEMORY_IDENTITY
    key_path = output / MEMORY_KEY_DIR / f"{subject}.key"
    registry = output / MEMORY_REGISTRY
    if force or not (key_path.is_file() and registry.is_file()):
        key_path.parent.mkdir(parents=True, exist_ok=True)
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
        )
        public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        key_path.write_text(_b64url(seed) + "\n", encoding="ascii")
        key_path.chmod(0o644)
        listed = ", ".join(f'"{name}"' for name in capabilities)
        registry.write_text(
            "# Memory service registry generated by scripts/dev_service_auth.py. Public key only.\n"
            "[[keys]]\n"
            f'kid = "{subject}-{KID_SUFFIX}"\n'
            f'sub = "{subject}"\n'
            f'public_key = "{_b64url(public)}"\n'
            f"capabilities = [{listed}]\n",
            encoding="utf-8",
        )
        registry.chmod(0o644)
        tomllib.loads(registry.read_text(encoding="utf-8"))
        written += [key_path, registry]
    db = output / MEMORY_DB_DIR
    db_files = [db / "postgres.env", db / "migrate.env", db / "service.env"]
    if force or not all(path.is_file() for path in db_files):
        migrator, service = secrets.token_hex(16), secrets.token_hex(16)  # 32 characters each
        _write_secret_env(db_files[0], {
            "POSTGRES_USER": BOOTSTRAP_USER, "POSTGRES_PASSWORD": secrets.token_hex(16),
            "MEMORY_DB_MIGRATOR_PASSWORD": migrator, "MEMORY_DB_SERVICE_PASSWORD": service,
        })
        _write_secret_env(db_files[1], {"MEMORY_MIGRATE_DB_PASSWORD": migrator})
        _write_secret_env(db_files[2], {"MEMORY_DB_PASSWORD": service})
        written += db_files
    tls = output / TLS_DIR
    tls_files = [tls / "ca" / "ca.pem", tls / "server" / "memory.pem", tls / "server" / "memory.key"]
    if force or not all(path.is_file() for path in tls_files):
        written += dev_memory_tls.generate(tls, force=True)
    return written


def generate(output: Path, *, force: bool = False) -> list[Path]:
    """Write the registry and the private seeds under ``output``; return the paths written.

    Refuses to overwrite existing files unless ``force`` is set, so a running stack keeps working.
    """
    keys_dir = output / "keys"
    targets = [output / "registry.toml"] + [keys_dir / f"{stem}.key" for _, stem, _ in IDENTITIES]
    existing = [path for path in targets if path.exists()]
    if existing and not force:
        raise FileExistsError(
            "already exists: " + ", ".join(str(p) for p in existing) + " (use --force to replace)"
        )
    keys_dir.mkdir(parents=True, exist_ok=True)
    blocks = [
        "# Development registry generated by scripts/dev_service_auth.py. Public keys only.\n"
    ]
    for subject, stem, capabilities in IDENTITIES:
        private = Ed25519PrivateKey.generate()
        seed = private.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        public = private.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        key_path = keys_dir / f"{stem}.key"
        key_path.write_text(_b64url(seed) + "\n", encoding="ascii")
        key_path.chmod(0o644)
        listed = ", ".join(f'"{name}"' for name in capabilities)
        blocks.append(
            "[[keys]]\n"
            f'kid = "{subject}-{KID_SUFFIX}"\n'
            f'sub = "{subject}"\n'
            f'public_key = "{_b64url(public)}"\n'
            f"capabilities = [{listed}]\n"
        )
    registry = output / "registry.toml"
    registry.write_text("\n".join(blocks), encoding="utf-8")
    registry.chmod(0o644)
    tomllib.loads(registry.read_text(encoding="utf-8"))  # a registry that does not parse never ships
    return targets + generate_memory(output, force=force)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="default: data/dev-auth")
    parser.add_argument("--force", action="store_true", help="replace existing keys and registry")
    args = parser.parse_args(argv)
    try:
        written = generate(args.output, force=args.force)
    except FileExistsError as error:
        # The main keys exist and are kept. An older checkout may still lack the memory files.
        added = generate_memory(args.output, force=args.force)
        for path in added:
            print(f"wrote {path}")
        if added:
            print("dev_service_auth: existing service keys kept; added the missing memory material.")
        print(f"dev_service_auth: {error}", file=sys.stderr)
        return 0 if added else 1
    for path in written:
        print(f"wrote {path}")
    print("Development keys, certificates and passwords only. Start the stack with: docker compose up --build -d")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
