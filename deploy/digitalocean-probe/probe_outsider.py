"""probe-outsider: a minimal component in a SECOND app that tries to reach the first app's
internal service (RUNBOOK fact 4). THROWAWAY.

For every candidate target it reports, stage by stage: resolves, TCP connects, a TLS handshake
completes (certificate NOT verified: the outsider has no reason to trust anything, it only asks
"can I reach it"), and whether a GET /__probe/health is answered by probe-service. For every
public URL it reports the HTTP status and whether the body looks like probe-service's health
answer, so reachability is not confused with "something else answered".

Expected on a correctly isolated internal service: nothing resolves, connects or answers.
Anything that does is a finding (the runbook says how to judge it).

Environment:
  PROBE_TARGET_HOSTS   comma-separated `host` or `host:port` of the FIRST app's internal service
                       (plain name, other name forms, private IP addresses the client reported)
  PROBE_TARGET_PORT    default port for entries without one (default 8443)
  PROBE_PUBLIC_URLS    comma-separated public URLs of the first app (https://...)
  PROBE_OUTSIDER_INTERVAL  seconds between repeats (default 600); one JSON line per run.
Retest R1 (RUNBOOK section 8b): a database denial check. When PROBE_OUTSIDER_DB_HOST is set, the
outsider (NOT a trusted source of the cluster, no database binding) tries the throwaway cluster
stage by stage: DNS, TCP connect, the PostgreSQL TLS handshake (SSLRequest, then TLS), and a login
with a throwaway credential. It reports where it stops (`classification`), never a secret:
  PROBE_OUTSIDER_DB_HOST      cluster host name (SECRET type in the spec: it is redacted in output)
  PROBE_OUTSIDER_DB_PORT      default 25060
  PROBE_OUTSIDER_DB_NAME      default `defaultdb` (not `probe`: the bootstrap revokes PUBLIC CONNECT
                              there, which would look like a refusal that the rule did not cause)
  PROBE_OUTSIDER_DB_USER / PROBE_OUTSIDER_DB_PASSWORD   a throwaway database user (SECRET type)
  PROBE_OUTSIDER_DB_CA_PEM    optional cluster CA (public); with it the login uses verify-full
Otherwise no secrets are used. Outbound calls go only to the configured targets, URLs and database.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import struct
import sys
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from probe_client import parse_host_port
from probe_common import (
    ConnectionInfo,
    DbError,
    DbGateway,
    PemError,
    PsycopgGateway,
    TempSecretFiles,
    decode_pem_variable,
    log_json,
    redact_host,
    scrub,
)

HEALTH_PATH = "/__probe/health"
POSTGRES_SSL_REQUEST = struct.pack("!II", 8, 80877103)  # length 8, the SSLRequest code
DEFAULT_DB_PORT = 25060
# Classifications of the database check, in the order a connection progresses.
DB_CLASSIFICATIONS = ("not_configured", "dns_failure", "timeout", "connection_refused", "tcp_error",
                      "tls_not_offered", "tls_failed", "tls_ok_authentication_refused",
                      "login_failed_other", "login_not_run", "login_ok_connected")


def _looks_like_probe(body: bytes) -> bool:
    try:
        return json.loads(body) == {"ok": True}
    except ValueError:
        return False


def probe_target(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    result: dict[str, Any] = {"host": host, "port": port, "resolves": False, "connects": False,
                              "tls_handshake": False, "answers_like_probe_service": False,
                              "reason": None}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        result["reason"] = f"dns: {exc.strerror or 'lookup failed'}"
        return result
    result["resolves"] = True
    result["addresses"] = sorted({i[4][0] for i in infos})
    try:
        sock = socket.create_connection((infos[0][4][0], port), timeout=timeout)
    except OSError as exc:
        result["reason"] = f"tcp: {exc.strerror or type(exc).__name__}"
        return result
    result["connects"] = True
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # reachability only; nothing secret is sent
    try:
        with context.wrap_socket(sock) as tls:
            result["tls_handshake"] = True
            tls.sendall(f"GET {HEALTH_PATH} HTTP/1.0\r\nHost: {host}\r\n\r\n".encode())
            data = b""
            while len(data) < 65536:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
            result["answers_like_probe_service"] = _looks_like_probe(data.partition(b"\r\n\r\n")[2])
    except (ssl.SSLError, OSError) as exc:
        result["reason"] = f"tls: {type(exc).__name__}"
    finally:
        sock.close()
    return result


def probe_public_url(url: str, timeout: float = 10.0) -> dict[str, Any]:
    parts = urlsplit(url)
    result: dict[str, Any] = {"url": f"{parts.scheme}://{parts.netloc}", "paths": []}
    if parts.scheme not in ("http", "https") or not parts.hostname:
        result["error"] = "not an http(s) URL"
        return result
    cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    for path in ("/", HEALTH_PATH):
        entry: dict[str, Any] = {"path": path}
        try:
            conn = cls(parts.hostname, parts.port, timeout=timeout)
            try:
                conn.request("GET", path)
                response = conn.getresponse()
                body = response.read(65536)
                entry.update(status=response.status,
                             answers_like_probe_service=_looks_like_probe(body))
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - every failure is a result
            entry["error"] = scrub(f"{type(exc).__name__}: {exc}")
        result["paths"].append(entry)
    return result


# ------------------------------------------------------------- retest R1: database denial


def probe_database(host: str, port: int, dbname: str, user: str, password: str,
                   ca_pem: str | None = None, *, timeout: float = 5.0,
                   create_connection: Callable[..., socket.socket] = socket.create_connection,
                   gateway_factory: Callable[[ConnectionInfo], DbGateway] = PsycopgGateway,
                   ) -> dict[str, Any]:
    """Try DNS, TCP, the PostgreSQL TLS handshake and a login; report exactly where it stops.

    `classification` is one of DB_CLASSIFICATIONS. Nothing secret is returned: the host is
    redacted, the user and password never appear, and every message is scrubbed. The TLS stage
    does not verify the certificate (it asks only "can I reach it"); the login stage verifies
    with the cluster CA when one is supplied, else uses sslmode=require.
    """
    hide = [host, user, password]
    stages: dict[str, Any] = {"dns": None, "tcp": None, "tls": None, "login": None}
    result: dict[str, Any] = {
        "check": "database_denial", "host_redacted": redact_host(host), "port": port,
        "database": dbname, "login_sslmode": "verify-full" if ca_pem else "require",
        "stages": stages, "classification": None, "stopped_at": None, "reached_database": False}

    def stop(stage: str, classification: str, reason: str) -> dict[str, Any]:
        stages[stage] = {"ok": False, "reason": scrub(reason, hide)}
        result.update(classification=classification, stopped_at=stage)
        return result

    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        return stop("dns", "dns_failure", exc.strerror or "lookup failed")
    stages["dns"] = {"ok": True, "address_count": len({i[4][0] for i in infos})}
    try:
        sock = create_connection((infos[0][4][0], port), timeout=timeout)
    except TimeoutError:
        return stop("tcp", "timeout", "no answer to the TCP connection within the timeout")
    except ConnectionRefusedError:
        return stop("tcp", "connection_refused", "the connection was actively refused")
    except OSError as exc:
        return stop("tcp", "tcp_error", f"{type(exc).__name__}: {exc.strerror or 'connect failed'}")
    stages["tcp"] = {"ok": True}
    try:
        sock.settimeout(timeout)
        try:
            sock.sendall(POSTGRES_SSL_REQUEST)
            answer = sock.recv(1)
        except TimeoutError:
            return stop("tls", "tls_failed", "connected, but no answer to the TLS request (timeout)")
        except OSError as exc:
            return stop("tls", "tls_failed", f"connection dropped during the TLS request: {type(exc).__name__}")
        if answer == b"":
            return stop("tls", "tls_failed", "the server closed the connection at the TLS request")
        if answer != b"S":
            return stop("tls", "tls_not_offered", "the server answered that it does not offer TLS")
        context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # reachability only; no credential is sent on this stage
        try:
            with context.wrap_socket(sock, server_hostname=None) as tls:
                stages["tls"] = {"ok": True, "tls_version": tls.version()}
        except (ssl.SSLError, OSError) as exc:
            return stop("tls", "tls_failed", f"handshake failed: {type(exc).__name__}")
    finally:
        sock.close()

    info = ConnectionInfo(scheme="postgresql", host=host, port=port, database=dbname, user=user,
                          password=password, sslmode=None, query_names=())
    try:
        with TempSecretFiles({"db-ca.crt": ca_pem} if ca_pem else {}) as paths:
            session = gateway_factory(info).connect(
                sslmode="verify-full" if ca_pem else "require",
                sslrootcert=str(paths["db-ca.crt"]) if ca_pem else None)
            try:
                session.execute("SELECT 1")
            finally:
                session.close()
    except DbError as exc:
        text = scrub(exc.message, hide)
        if exc.sqlstate is None and "not installed" in exc.message:
            stages["login"] = {"ok": False, "reason": text}
            result.update(classification="login_not_run", stopped_at="login")
        elif (exc.sqlstate or "").startswith("28"):
            stages["login"] = {"ok": False, "sqlstate": exc.sqlstate, "reason": text}
            result.update(classification="tls_ok_authentication_refused", stopped_at="login")
        else:
            stages["login"] = {"ok": False, "sqlstate": exc.sqlstate, "reason": text}
            result.update(classification="login_failed_other", stopped_at="login")
        return result
    stages["login"] = {"ok": True}
    result.update(classification="login_ok_connected", reached_database=True)
    return result


def database_check(environ: Mapping[str, str], **kwargs: Any) -> dict[str, Any]:
    """Run `probe_database` from the PROBE_OUTSIDER_DB_* variables, or say it is not configured."""
    host = environ.get("PROBE_OUTSIDER_DB_HOST", "").strip()
    user = environ.get("PROBE_OUTSIDER_DB_USER", "")
    password = environ.get("PROBE_OUTSIDER_DB_PASSWORD", "")
    base: dict[str, Any] = {"check": "database_denial", "classification": "not_configured",
                            "reached_database": False}
    if not host:
        return base | {"note": "PROBE_OUTSIDER_DB_HOST is not set"}
    if not (user and password):
        return base | {"note": "PROBE_OUTSIDER_DB_USER or PROBE_OUTSIDER_DB_PASSWORD is not set"}
    try:
        port = int(environ.get("PROBE_OUTSIDER_DB_PORT", "") or DEFAULT_DB_PORT)
    except ValueError:
        return base | {"note": "PROBE_OUTSIDER_DB_PORT is not a number"}
    ca_pem: str | None = None
    ca_note = None
    if environ.get("PROBE_OUTSIDER_DB_CA_PEM", "").strip():
        try:
            ca_pem, _ = decode_pem_variable(environ["PROBE_OUTSIDER_DB_CA_PEM"], "PROBE_OUTSIDER_DB_CA_PEM")
        except PemError as exc:
            ca_note = str(exc)
    result = probe_database(host, port, environ.get("PROBE_OUTSIDER_DB_NAME", "") or "defaultdb",
                            user, password, ca_pem, **kwargs)
    if ca_note:
        result["ca_note"] = ca_note
    return result


def run_outsider(environ: Mapping[str, str]) -> dict[str, Any]:
    default_port = int(environ.get("PROBE_TARGET_PORT", "8443"))
    targets: list[dict[str, Any]] = []
    for item in environ.get("PROBE_TARGET_HOSTS", "").split(","):
        if item.strip():
            try:
                host, port = parse_host_port(item, default_port)
            except ValueError:
                targets.append({"host": item.strip(), "port": 0, "error": "unparseable entry"})
                continue
            targets.append(probe_target(host, port))
    urls = [probe_public_url(u.strip()) for u in environ.get("PROBE_PUBLIC_URLS", "").split(",")
            if u.strip()]

    def names(key: str) -> list[str]:
        return [f"{t['host']}:{t['port']}" for t in targets if t.get(key)]

    reached = names("answers_like_probe_service")
    public_hits = [u["url"] for u in urls
                   if any(p.get("answers_like_probe_service") for p in u["paths"])]
    database = database_check(environ)
    return {
        "probe": "outsider", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targets": targets, "public_urls": urls, "database": database,
        "summary": {
            "database_classification": database["classification"],
            "database_reached": database["reached_database"],
            "targets_resolving": names("resolves"),
            "targets_connecting": names("connects"),
            "targets_answering_as_probe_service": reached,
            "public_urls_answering_as_probe_service": public_hits,
            "isolated": not reached and not public_hits,
        },
    }


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    interval = int(env.get("PROBE_OUTSIDER_INTERVAL", "600"))
    while True:
        log_json("probe_outsider_summary", **run_outsider(env))
        if interval <= 0:
            return 0
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
