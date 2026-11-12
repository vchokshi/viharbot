"""A minimal migration runner: numbered plain ``.sql`` files, applied once, in order.

Run as the migration role only (``python -m viharbot.memory_service.migrate``); the service role
cannot create or alter anything. Each file runs in its own transaction together with its
bookkeeping row, under an advisory lock so two runners cannot interleave. An applied file whose
text later changes is refused (checksum), so history is never silently rewritten. There are no
down migrations: a change is a new numbered file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import Mapping

import psycopg

logger = logging.getLogger("memory_migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_ID = 7_040_001  # arbitrary, stable: "issue 40, migration lock"


_SCHEMA_NAME = "memory"
# The one statement a migration file may carry that needs database-level CREATE. Matched exactly,
# on a line of its own, so nothing else in a migration is ever rewritten.
_CREATE_SCHEMA_LINE = re.compile(r"^CREATE SCHEMA IF NOT EXISTS memory;[ \t]*$", re.MULTILINE)


class MigrationError(RuntimeError):
    """A migration cannot be applied safely. Nothing further is run."""


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, str, str]]:
    """(version, filename, text) in order. Rejects stray names, gaps and duplicates."""
    found: list[tuple[int, str, str]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _NAME.fullmatch(path.name)
        if not match:
            raise MigrationError(f"migration file name is not NNNN_name.sql: {path.name}")
        found.append((int(match.group(1)), path.name, path.read_text(encoding="utf-8")))
    versions = [version for version, _, _ in found]
    if versions != list(range(1, len(versions) + 1)):
        raise MigrationError("migration numbers must be 1, 2, 3 ... with no gap or repeat")
    return found


def schema_exists(conn: psycopg.Connection, name: str = _SCHEMA_NAME) -> bool:
    return conn.execute("SELECT 1 FROM pg_namespace WHERE nspname = %s", (name,)).fetchone() is not None


def skip_schema_create(text: str) -> str:
    """Migration text with its ``CREATE SCHEMA IF NOT EXISTS memory;`` line made a comment.

    PostgreSQL checks the CREATE privilege on the database before it looks at IF NOT EXISTS, so
    that statement fails for a migrator that owns the schema but not the database (DigitalOcean
    Managed PostgreSQL: the platform owns the database, the bootstrap job creates the schema and
    makes the migrator its owner; founder decision N6). The checksum is always taken over the
    ORIGINAL file text, so databases that already applied the file are unaffected.
    """
    return _CREATE_SCHEMA_LINE.sub("-- schema already exists: CREATE SCHEMA skipped by the runner", text)


def migrate(conninfo: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration. Returns the file names applied."""
    migrations = discover(directory)
    applied_now: list[str] = []
    with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            # Create the schema only when it is missing (a fresh database the migrator owns, as in
            # Compose). When it exists, no database-level CREATE is needed or used. A migrator that
            # lacks it still cannot create any other schema.
            schema_present = schema_exists(conn)
            if not schema_present:
                conn.execute("CREATE SCHEMA IF NOT EXISTS memory")
                schema_present = True
            conn.execute(
                "CREATE TABLE IF NOT EXISTS memory.schema_migrations ("
                "version integer PRIMARY KEY, name text NOT NULL, checksum text NOT NULL, "
                "applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {
                row[0]: row[1]
                for row in conn.execute("SELECT version, checksum FROM memory.schema_migrations")
            }
            for version, name, text in migrations:
                checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if version in done:
                    if done[version] != checksum:
                        raise MigrationError(f"{name} was changed after it was applied")
                    continue
                with conn.transaction():
                    conn.execute(skip_schema_create(text) if schema_present else text)  # type: ignore[arg-type]  # plain multi-statement SQL, no parameters
                    conn.execute(
                        "INSERT INTO memory.schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                        (version, name, checksum),
                    )
                applied_now.append(name)
                logger.info("applied migration", extra={"agent": "memory", "migration": name})
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now


def main(environ: Mapping[str, str] | None = None) -> int:
    from .app import conninfo_from_environment

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    env = os.environ if environ is None else environ
    try:
        applied = migrate(conninfo_from_environment(env, prefix="MEMORY_MIGRATE_DB_"))
    except (MigrationError, psycopg.Error, RuntimeError) as exc:
        # The class, not the message: a driver message may carry connection details.
        print(f"migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(f"migrations applied: {', '.join(applied) if applied else 'none (up to date)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
