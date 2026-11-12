"""Shared helpers for the throwaway DigitalOcean probe kit (see RUNBOOK.md).

THROWAWAY: this kit answers questions about DigitalOcean (internal routing, internal TLS, role
bootstrap, pre-deploy jobs, database TLS). It is not part of ViharBot's product and is deleted or
ignored once the answers are recorded. Nothing here contacts DigitalOcean's API; the only network
calls in the kit are the documented probe-to-probe calls, the job's database connection, and the
outsider's reachability attempts.

Rules this module enforces for every probe component:
  * No secret value is ever printed or returned: connection info is reduced to shapes and names,
    and every error message passes through `scrub` before it is reported.
  * Secrets that must be files (certificate, key, CA) are written 0600 into a private temporary
    directory and removed as soon as the `with` block ends.
  * Database access goes through the small `DbGateway` interface so the logic can be tested with
    a fake and no database.

Standard library only, plus psycopg (imported lazily, only by the real gateway).
"""

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
import re
import shutil
import ssl
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence
from urllib.parse import parse_qsl, unquote, urlsplit

# --------------------------------------------------------------------------------------- logging


def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def log_json(event: str, **fields: Any) -> None:
    """One JSON object per line on stdout. Callers pass only non-secret, already scrubbed fields."""
    print(json.dumps({"event": event, "ts": utc_now_iso(), **fields}, sort_keys=True, default=str),
          flush=True)


# ---------------------------------------------------------------------------------- redaction

# Environment variable NAMES containing any of these are never reported by value and never used
# as a source of hostnames. (Names alone are reported; names are not secrets.)
SENSITIVE_NAME_PARTS = ("PASSWORD", "SECRET", "TOKEN", "KEY", "PEM", "CERT", "DATABASE", "DB_",
                        "PGPASS", "CREDENTIAL", "AUTH")

_URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*://[^\s'\"<>]+")
_KV_SECRET_RE = re.compile(r"(?i)\b(password|passwd|pwd|sslpassword)\s*=\s*\S+")


def is_sensitive_name(name: str) -> bool:
    upper = name.upper()
    return any(part in upper for part in SENSITIVE_NAME_PARTS)


def scrub(text: object, known_values: Sequence[str | None] = (), limit: int | None = 600) -> str:
    """Return `text` with every known secret value, any URL and any `password=` pair removed.

    Used on every error message before it is logged or returned. Values shorter than 3 characters
    are not replaced (they would mangle ordinary words), but URLs and key=value pairs always are.
    """
    out = str(text)
    for value in sorted({v for v in known_values if v and len(v) >= 3}, key=len, reverse=True):
        out = out.replace(value, "***")
    out = _URL_RE.sub("<url withheld>", out)
    out = _KV_SECRET_RE.sub(lambda m: f"{m.group(1)}=***", out)
    return out if limit is None else out[:limit]


def scrub_tree(value: Any, known_values: Sequence[str | None] = ()) -> Any:
    """`scrub` every string inside a JSON-like structure (dict keys are left alone)."""
    if isinstance(value, str):
        return scrub(value, known_values, limit=None)
    if isinstance(value, dict):
        return {k: scrub_tree(v, known_values) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [scrub_tree(v, known_values) for v in value]
    return value


def env_names(environ: Mapping[str, str]) -> list[str]:
    """Sorted variable names only. Values are never read."""
    return sorted(environ)


# ----------------------------------------------------------------------- PEM from a variable


class PemError(ValueError):
    """A PEM variable is missing or unusable. The message never contains the value."""


_PEM_RE = re.compile(r"(-----BEGIN ([A-Z0-9 ]+)-----)\s*(.*?)\s*(-----END \2-----)", re.S)


def decode_pem_variable(value: str | None, what: str) -> tuple[str, str]:
    """Turn an environment variable into PEM text; return (pem, form).

    Accepts the PEM as pasted (real newlines), with literal backslash-n sequences, with newlines
    flattened to spaces (some control panels do this), or base64 of the PEM. `form` is one of
    "raw", "reflowed" or "base64" and is reported so the founder learns what the platform kept.
    """
    if value is None or not value.strip():
        raise PemError(f"{what} is not set or is empty")
    text = value.strip()
    form = "raw"
    if "-----BEGIN" not in text:
        try:
            text = base64.b64decode(text, validate=True).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise PemError(f"{what} is neither PEM text nor base64 of PEM (value withheld)") from None
        form = "base64"
        if "-----BEGIN" not in text:
            raise PemError(f"{what} does not decode to PEM (value withheld)")
    original = text
    text = text.replace("\\n", "\n")
    match = _PEM_RE.search(text)
    if not match:
        raise PemError(f"{what} has a PEM header but no matching footer (value withheld)")
    body = re.sub(r"\s+", "", match.group(3))
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    pem = f"{match.group(1)}\n{wrapped}\n{match.group(4)}\n"
    if form == "raw" and original.strip().replace("\r\n", "\n") != pem.strip():
        form = "reflowed"
    return pem, form


def pem_sha256_fingerprint(pem: str) -> str:
    """SHA-256 of the first certificate in `pem` (public information, safe to report)."""
    der = ssl.PEM_cert_to_DER_cert(pem[pem.index("-----BEGIN CERTIFICATE-----"):])
    return hashlib.sha256(der).hexdigest()


# ------------------------------------------------------------------------- secret temp files


class TempSecretFiles:
    """Write named secrets to files for the duration of a `with` block.

    The directory is created 0700 and each file 0600 (O_EXCL, then fchmod, so the umask cannot
    loosen it). Everything is removed on exit, including when the block raises. Paths are never
    logged by this class.
    """

    def __init__(self, contents: Mapping[str, str]) -> None:
        self._contents = dict(contents)
        self._dir: Path | None = None

    def __enter__(self) -> dict[str, Path]:
        self._dir = Path(tempfile.mkdtemp(prefix="probe-secret-"))
        os.chmod(self._dir, 0o700)
        paths: dict[str, Path] = {}
        try:
            for name, content in self._contents.items():
                path = self._dir / name
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, content.encode("utf-8"))
                finally:
                    os.close(fd)
                paths[name] = path
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return paths

    def __exit__(self, *exc_info: object) -> None:
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None


# ------------------------------------------------------------------- connection information


class ConnectionInfoError(ValueError):
    """The connection URL could not be used. The message is fixed text: it never echoes the URL."""


def redact_host(host: str) -> str:
    """Reduce a hostname to its shape: keep the last three labels, hide the rest."""
    if not host:
        return ""
    if re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", host):
        return host.split(".")[0] + ".*.*.*"
    if ":" in host:  # IPv6 literal
        return "[ipv6 withheld]"
    labels = host.split(".")
    if len(labels) <= 3:
        return "***"
    return "***." + ".".join(labels[-3:])


@dataclass(frozen=True)
class ConnectionInfo:
    """Parsed database connection information. Secrets are fields that never appear in repr."""

    scheme: str
    host: str = field(repr=False)
    port: int | None
    database: str
    user: str = field(repr=False)
    password: str = field(repr=False)
    sslmode: str | None
    query_names: tuple[str, ...]

    def secret_values(self) -> list[str]:
        return [v for v in (self.password, self.user, self.host) if v]

    def public(self) -> dict[str, Any]:
        """The only form that is ever logged or returned: names, shapes and non-secret fields."""
        return {
            "scheme": self.scheme,
            "host_redacted": redact_host(self.host),
            "host_has_private_prefix": self.host.lower().startswith("private-"),
            "port": self.port,
            "database": self.database,
            "sslmode_in_url": self.sslmode if self.sslmode is not None else "(absent)",
            "user_present": bool(self.user),
            "password_present": bool(self.password),
            "query_parameter_names": list(self.query_names),
        }

    def __str__(self) -> str:  # safe by construction
        return f"ConnectionInfo({json.dumps(self.public(), sort_keys=True)})"


def parse_connection_info(url: str | None) -> ConnectionInfo:
    if not url or not url.strip():
        raise ConnectionInfoError("connection URL is not set or is empty")
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        if scheme not in ("postgres", "postgresql"):
            raise ConnectionInfoError("connection URL scheme is not postgres or postgresql")
        host = parts.hostname or ""
        port = parts.port
        user = unquote(parts.username) if parts.username else ""
        password = unquote(parts.password) if parts.password else ""
        query = parse_qsl(parts.query, keep_blank_values=True)
        database = unquote(parts.path.lstrip("/"))
    except ConnectionInfoError:
        raise
    except ValueError:
        raise ConnectionInfoError("connection URL could not be parsed (value withheld)") from None
    if not host:
        raise ConnectionInfoError("connection URL has no host (value withheld)")
    sslmode = next((v for k, v in query if k == "sslmode"), None)
    return ConnectionInfo(scheme=scheme, host=host, port=port, database=database, user=user,
                          password=password, sslmode=sslmode,
                          query_names=tuple(sorted({k for k, _ in query})))


# --------------------------------------------------------------------------- SQL quoting


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# -------------------------------------------------------------------------- database interface


class DbError(Exception):
    """A database failure with a SQLSTATE (when known) and an already scrubbed message."""

    def __init__(self, sqlstate: str | None, message: str) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message


class DbSession(Protocol):
    def execute(self, sql: str) -> list[tuple[Any, ...]]: ...

    def close(self) -> None: ...


class DbGateway(Protocol):
    def connect(self, *, sslmode: str, sslrootcert: str | None = None, user: str | None = None,
                password: str | None = None) -> DbSession: ...


class _PsycopgSession:
    def __init__(self, conn: Any, secrets: list[str]) -> None:
        self._conn = conn
        self._secrets = secrets

    def execute(self, sql: str) -> list[tuple[Any, ...]]:
        import psycopg

        try:
            cur = self._conn.execute(sql)
            return [tuple(row) for row in cur.fetchall()] if cur.description else []
        except psycopg.Error as exc:
            raise DbError(getattr(exc, "sqlstate", None), scrub(exc, self._secrets)) from None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001 - closing must never raise out of a probe
            pass


class PsycopgGateway:
    """The real gateway: one autocommit psycopg connection per `connect`."""

    def __init__(self, info: ConnectionInfo, connect_timeout: int = 10) -> None:
        self._info = info
        self._timeout = connect_timeout

    def connect(self, *, sslmode: str, sslrootcert: str | None = None, user: str | None = None,
                password: str | None = None) -> DbSession:
        try:
            import psycopg
        except ImportError:
            raise DbError(None, "psycopg is not installed in this image") from None
        info = self._info
        secrets = info.secret_values() + [v for v in (user, password) if v]
        kwargs: dict[str, Any] = {
            "host": info.host, "port": info.port, "dbname": info.database,
            "user": user or info.user, "password": password or info.password,
            "sslmode": sslmode, "connect_timeout": self._timeout, "autocommit": True,
        }
        if sslrootcert:
            kwargs["sslrootcert"] = sslrootcert
        try:
            conn = psycopg.connect(**kwargs)
        except psycopg.Error as exc:
            raise DbError(getattr(exc, "sqlstate", None), scrub(exc, secrets)) from None
        return _PsycopgSession(conn, secrets)


# --------------------------------------------------------------------------------- the marker

MARKER_TABLES = ("probe_scratch.probe_marker", "public.probe_marker")


def read_marker_attempts(session: DbSession, table: str) -> dict[str, Any]:
    """Counts of marker rows grouped by (tag, mode) and the rows in write order.

    Works on the extended marker table (RUNBOOK retest R3). On a table without the tag columns
    (the 2026-11-12 shape) it reports `tag_columns: false` instead of failing.
    """
    try:
        groups = session.execute(
            f"SELECT coalesce(job_tag, '(none)'), job_mode, count(*) FROM {table} "
            "GROUP BY coalesce(job_tag, '(none)'), job_mode ORDER BY 1, 2")
        rows = session.execute(
            f"SELECT coalesce(job_tag, '(none)'), job_mode, coalesce(started_at, written_at::text), "
            f"coalesce(attempt_id, ''), run_number, coalesce(platform_ids, '') FROM {table} "
            "ORDER BY run_number, written_at")
    except DbError as exc:
        return {"tag_columns": False, "tag_columns_error": exc.sqlstate or "error"}
    attempts: list[dict[str, Any]] = []
    for tag, mode, started, attempt_id, run_number, platform in rows:
        try:
            ids = json.loads(platform) if platform else {}
        except ValueError:
            ids = {}
        attempts.append({"tag": tag, "mode": mode, "started_at": str(started),
                         "attempt_id": attempt_id, "run_number": int(run_number),
                         "platform_identifiers": ids})
    return {"tag_columns": True,
            "by_tag_mode": [{"tag": t, "mode": m, "count": int(c)} for t, m, c in groups],
            "attempts": attempts}


def read_marker(session: DbSession) -> dict[str, Any]:
    """Report whether the job's marker rows exist (names, counts, tags and times only)."""
    errors: list[str] = []
    for table in MARKER_TABLES:
        try:
            count, last_run, written = session.execute(
                f"SELECT count(*), max(run_number), max(written_at)::text FROM {table}")[0]
            mode_rows = session.execute(
                f"SELECT job_mode FROM {table} ORDER BY run_number DESC LIMIT 1")
        except DbError as exc:
            errors.append(f"{table}: {exc.sqlstate or 'error'}")
            continue
        result: dict[str, Any] = {
            "found": bool(count), "table": table, "rows": int(count),
            "latest_run_number": last_run, "latest_written_at": written,
            "latest_job_mode": mode_rows[0][0] if mode_rows else None}
        result.update(read_marker_attempts(session, table))
        return result
    return {"found": False, "table": None, "rows": 0, "lookup_errors": errors}


def fail_hard(message: str, code: int = 2) -> int:
    """Print a fixed message on stderr (never a secret) and return an exit code."""
    print(message, file=sys.stderr, flush=True)
    return code
