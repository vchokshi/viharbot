"""probe-client: the second component (RUNBOOK facts 2, 3 and the ordering half of 5). THROWAWAY.

At startup it, in a background thread:
  1. builds a candidate list of internal hostnames for probe-service (PROBE_CANDIDATE_HOSTS, plus
     hostnames suggested by the app's own environment and DNS search domains) and, for each,
     reports: resolves or not, TCP connects or not, certificate verifies or not and why. TLS
     verification is ON and trusts ONLY the pinned certificate in PROBE_TLS_CA_PEM;
  2. sends tricky requests to the first host that verifies and compares what the service says it
     received with what was sent (method, raw target, body hash and length);
  3. records, at the first health check and at the first non-health request, whether the job's
     marker row exists (the pre-deploy ordering test).
It then logs ONE JSON summary line and serves it read-only at GET /results (bearer token
PROBE_RESULTS_TOKEN; without a token the endpoint answers 503, never open). GET /health is open.

Nothing here prints a secret: results hold hostnames the founder configured, resolved private
addresses, certificate fingerprints (public) and names of environment variables, never values of
secret ones. Outbound calls: only to the candidate hosts, and the database marker read.

If no candidate verifies, echo checks still run over TLS with verification off, labelled
"unverified-fallback" in the results, so routing can be judged separately from TLS. No secret is
sent in either case.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import socket
import ssl
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from probe_common import (
    DbError,
    DbGateway,
    PemError,
    PsycopgGateway,
    ConnectionInfoError,
    decode_pem_variable,
    env_names,
    is_sensitive_name,
    log_json,
    parse_connection_info,
    pem_sha256_fingerprint,
    read_marker,
    scrub,
    utc_now_iso,
)

DEFAULT_PORT = 8443
RESULTS_SCHEMA = 1


# ---------------------------------------------------------------------------- candidate hosts


def parse_host_port(spec: str, default_port: int) -> tuple[str, int]:
    """`host`, `host:port` or `[v6]:port` -> (host, port). Raises ValueError on nonsense."""
    spec = spec.strip()
    if spec.startswith("["):
        host, _, rest = spec[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    if spec.count(":") == 1:
        host, _, port = spec.partition(":")
        return host, int(port)
    return spec, default_port


def resolv_search_domains(path: str = "/etc/resolv.conf") -> list[str]:
    try:
        lines = Path(path).read_text().splitlines()
    except OSError:
        return []
    domains: list[str] = []
    for line in lines:
        parts = line.split()
        if parts and parts[0] in ("search", "domain"):
            domains = parts[1:]
    return domains


def build_candidates(environ: Mapping[str, str], default_port: int = DEFAULT_PORT,
                     search_domains: list[str] | None = None) -> list[dict[str, Any]]:
    """Candidate (host, port, source) entries. Order: configured, then environment hints.

    PROBE_CANDIDATE_HOSTS is a comma-separated list of `host` or `host:port`. Hints (flagged by
    `source`): `probe-service.<search domain>` for each DNS search domain, and the hostname of any
    non-sensitive variable whose name ends in _URL, _DOMAIN, _HOSTNAME or _PRIVATE_URL (the
    platform's injected variables, if any). Duplicates are dropped.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()

    def add(host: str, port: int, source: str) -> None:
        host = host.strip().rstrip(".")
        if host and (host, port) not in seen:
            seen.add((host, port))
            out.append({"host": host, "port": port, "source": source})

    configured = environ.get("PROBE_CANDIDATE_HOSTS", "").strip() or "probe-service"
    for item in configured.split(","):
        if item.strip():
            try:
                host, port = parse_host_port(item, default_port)
            except ValueError:
                continue
            add(host, port, "PROBE_CANDIDATE_HOSTS")
    for domain in (resolv_search_domains() if search_domains is None else search_domains):
        add(f"probe-service.{domain}", default_port, "resolv.conf search domain")
    for name, value in sorted(environ.items()):
        if is_sensitive_name(name) or not name.upper().endswith(
                ("_URL", "_DOMAIN", "_HOSTNAME", "_PRIVATE_URL")):
            continue
        host = urlsplit(value if "://" in value else "//" + value).hostname
        if host:
            add(host, default_port, f"env:{name}")
    return out


# ------------------------------------------------------------------------------------- TLS


def build_pinned_context(ca_pem: str, *, check_hostname: bool = True) -> ssl.SSLContext:
    """Verification ON, trusting only `ca_pem` (the pinned certificate), no system roots."""
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cadata=ca_pem)
    context.check_hostname = check_hostname
    context.verify_mode = ssl.CERT_REQUIRED
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def check_host(host: str, port: int, ca_pem: str, *, server_name: str | None = None,
               timeout: float = 5.0) -> dict[str, Any]:
    """Resolve, connect and verify against the pinned certificate; report each stage.

    `server_name` is the name verified against the certificate (default: `host`); tests use it to
    connect to a loopback address while verifying a different name.
    """
    result: dict[str, Any] = {"host": host, "port": port, "resolves": False, "addresses": [],
                              "connects": False, "tls_handshake": False, "verified": False,
                              "reason": None, "chain_ok_ignoring_hostname": None}
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        result["reason"] = f"dns: {exc.strerror or 'lookup failed'}"
        return result
    result["resolves"] = True
    result["addresses"] = sorted({info[4][0] for info in infos})
    name = server_name or host
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection((infos[0][4][0], port), timeout=timeout)
        result["connects"] = True
    except OSError as exc:
        result["reason"] = f"tcp: {exc.strerror or type(exc).__name__}"
        return result
    try:
        with build_pinned_context(ca_pem).wrap_socket(sock, server_hostname=name) as tls:
            result.update(tls_handshake=True, verified=True, tls_version=tls.version())
        return result
    except ssl.SSLCertVerificationError as exc:
        result["reason"] = f"certificate: {exc.verify_message}"
        result["tls_handshake"] = False
    except (ssl.SSLError, OSError) as exc:
        result["reason"] = f"tls: {type(exc).__name__}: {getattr(exc, 'reason', None) or exc.args[:1]}"
        return result
    finally:
        sock.close()
    # Certificate rejected: is the chain fine and only the NAME wrong? (distinguishes a wrong name
    # from a different certificate or something intercepting the connection)
    try:
        with socket.create_connection((infos[0][4][0], port), timeout=timeout) as raw:
            with build_pinned_context(ca_pem, check_hostname=False).wrap_socket(raw) as tls:
                result["chain_ok_ignoring_hostname"] = True
                result["tls_version"] = tls.version()
    except (ssl.SSLError, OSError):
        result["chain_ok_ignoring_hostname"] = False
    return result


# --------------------------------------------------------------------------- echo comparison


def deterministic_bytes(count: int, label: str) -> bytes:
    """Reproducible pseudo-random bytes (no secrets): sha256 counter blocks."""
    out = bytearray()
    block = 0
    while len(out) < count:
        out += hashlib.sha256(f"{label}:{block}".encode()).digest()
        block += 1
    return bytes(out[:count])


@dataclass(frozen=True)
class SentRequest:
    name: str
    method: str
    target: str
    body: bytes | None = None
    content_type: str | None = None
    extra_headers: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def build_cases(large_kb: int = 300) -> list[SentRequest]:
    nonascii = "héllo wörld ✓ 日本語 🚀".encode() + bytes(range(256))
    return [
        SentRequest("get-plain", "GET", "/echo/plain"),
        SentRequest("get-percent-space", "GET", "/echo/a%20b/c?x=1"),
        SentRequest("get-query-order-and-repeat", "GET", "/echo?b=2&a=1&a=0"),
        SentRequest("get-query-order-reversed", "GET", "/echo?a=0&a=1&b=2"),
        SentRequest("get-trailing-empty-query", "GET", "/echo/x?"),
        SentRequest("get-bare-and-empty-values", "GET", "/echo?a&a=&b=&=c"),
        SentRequest("get-hex-case-upper", "GET", "/echo/%E4%2F%7E?q=%E4%7E%2F"),
        SentRequest("get-hex-case-lower", "GET", "/echo/%e4%2f%7e?q=%e4%7e%2f"),
        SentRequest("get-plus-vs-percent", "GET", "/echo?q=a+b&r=a%20b"),
        SentRequest("get-path-not-normalized", "GET", "/echo//double//slash/./dot/../x"),
        SentRequest("get-utf8-percent", "GET", "/echo/%E2%9C%93?name=%E6%97%A5%E6%9C%AC"),
        SentRequest("post-empty", "POST", "/echo/post", b""),
        SentRequest("post-small-json", "POST", "/echo/post?kind=json",
                    json.dumps({"a": 1, "b": ["x", "y"], "n": None}).encode(), "application/json"),
        SentRequest(f"post-large-{large_kb}kb", "POST", "/echo/large",
                    deterministic_bytes(large_kb * 1024, "large"), "application/octet-stream"),
        SentRequest("post-non-ascii", "POST", "/echo/bytes", nonascii,
                    "application/octet-stream"),
        SentRequest("put-body", "PUT", "/echo/put?x=1", b"put-body", "text/plain"),
        SentRequest("delete-empty", "DELETE", "/echo/del?id=7"),
        SentRequest("patch-body", "PATCH", "/echo/patch", b'{"p":1}', "application/json"),
    ]


def compare_exchange(sent: SentRequest, received: Mapping[str, Any], *,
                     sent_host_header: str | None = None) -> dict[str, Any]:
    """Compare what was sent with what the service reports it received.

    `exact` is true only when method, raw target, body SHA-256 and body length all match
    byte-for-byte: those are the fields ADR-006 binds. Header differences are reported as notes
    (a proxy adding or lowercasing headers does not break the binding).
    """
    body = sent.body or b""
    diffs: list[dict[str, Any]] = []

    def check(field_name: str, want: Any, got: Any) -> None:
        if want != got:
            diffs.append({"field": field_name, "sent": want, "received": got})

    check("method", sent.method, received.get("method"))
    check("raw_target", sent.target, received.get("raw_target"))
    check("body_sha256", hashlib.sha256(body).hexdigest(), received.get("body_sha256"))
    check("body_length", len(body), received.get("body_length"))

    notes: list[str] = []
    safe = received.get("safe_headers") or {}
    if sent.content_type and safe.get("content-type") != sent.content_type:
        notes.append(f"content-type changed: sent {sent.content_type!r}, received "
                     f"{safe.get('content-type')!r}")
    if sent.body is not None and safe.get("content-length") not in (None, str(len(body))):
        notes.append(f"content-length changed: sent {len(body)}, received {safe.get('content-length')!r}")
    if received.get("body_framing") == "chunked":
        notes.append("body arrived chunked (the platform re-framed it)")
    if sent_host_header and safe.get("host") != sent_host_header:
        notes.append(f"host header changed: sent {sent_host_header!r}, received {safe.get('host')!r}")
    sent_names = {"host", "content-type", "content-length"} | {n.lower() for n, _ in sent.extra_headers}
    got_names = {str(n).lower() for n in received.get("header_names", [])}
    added = sorted(got_names - sent_names - {"accept-encoding"})
    if added:
        notes.append("headers added in transit (names only): " + ", ".join(added))
    return {"case": sent.name, "method": sent.method, "sent_target": sent.target,
            "body_length": len(body), "exact": not diffs, "diffs": diffs, "notes": notes}


def send_request(sent: SentRequest, host: str, port: int, context: ssl.SSLContext | None,
                 timeout: float = 20.0) -> dict[str, Any]:
    """Send `sent` with its target byte-for-byte as written and return the parsed echo."""
    conn: http.client.HTTPConnection
    if context is None:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPSConnection(host, port, timeout=timeout, context=context)
    try:
        conn.putrequest(sent.method, sent.target, skip_accept_encoding=True)
        if sent.content_type:
            conn.putheader("Content-Type", sent.content_type)
        if sent.body is not None:
            conn.putheader("Content-Length", str(len(sent.body)))
        for name, value in sent.extra_headers:
            conn.putheader(name, value)
        conn.endheaders(sent.body if sent.body else None)
        response = conn.getresponse()
        data = response.read()
        if response.status != 200:
            raise RuntimeError(f"unexpected status {response.status}")
        return json.loads(data)
    finally:
        conn.close()


def run_echo_suite(host: str, port: int, context: ssl.SSLContext | None, *,
                   large_kb: int = 300) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    host_header = host if port in (80, 443) else f"{host}:{port}"
    for case in build_cases(large_kb):
        case = SentRequest(case.name, case.method, case.target, case.body, case.content_type,
                           (("X-Probe-Case", case.name),))
        try:
            received = send_request(case, host, port, context)
            results.append(compare_exchange(case, received, sent_host_header=host_header))
        except Exception as exc:  # noqa: BLE001 - every failure is a result, not a crash
            results.append({"case": case.name, "method": case.method, "sent_target": case.target,
                            "exact": False, "error": scrub(f"{type(exc).__name__}: {exc}")})
    return results


# ------------------------------------------------------------------------------- ordering


class OrderingObserver:
    """Reads the job's marker row the first time each named event happens (once per name)."""

    def __init__(self, gateway: DbGateway | None, sslmode: str = "require") -> None:
        self._gateway = gateway
        self._sslmode = sslmode
        self._lock = threading.Lock()
        self.observations: dict[str, dict[str, Any]] = {}

    def observe(self, label: str) -> None:
        with self._lock:
            if label in self.observations:
                return
            record: dict[str, Any] = {"at": utc_now_iso()}
            self.observations[label] = record  # claim first so concurrent requests do not repeat
        if self._gateway is None:
            record["marker"] = {"found": None, "note": "no database configured"}
            return
        try:
            session = self._gateway.connect(sslmode=self._sslmode)
            try:
                record["marker"] = read_marker(session)
            finally:
                session.close()
        except DbError as exc:
            record["marker"] = {"found": None, "error": exc.message, "sqlstate": exc.sqlstate}


# ---------------------------------------------------------------------------- the whole run


@dataclass
class ProbeState:
    results: dict[str, Any] = field(default_factory=lambda: {"probe": "client", "status": "starting"})
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return json.loads(json.dumps(self.results, default=str))


def wait_for_service(candidates: list[dict[str, Any]], wait_seconds: float,
                     sleep: Callable[[float], None] = time.sleep, interval: float = 3.0) -> None:
    """Give the service time to start: return once any candidate accepts a TCP connection."""
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        for cand in candidates:
            try:
                socket.create_connection((cand["host"], cand["port"]), timeout=2).close()
                return
            except OSError:
                continue
        sleep(interval)


def run_probe(environ: Mapping[str, str], state: ProbeState, *,
              wait_seconds: float | None = None) -> dict[str, Any]:
    started = utc_now_iso()
    candidates = build_candidates(environ)
    results: dict[str, Any] = {
        "probe": "client", "schema": RESULTS_SCHEMA, "status": "running", "started_at": started,
        "environment_variable_names": env_names(environ), "candidates": candidates,
    }
    with state.lock:
        state.results.update(results)
    try:
        ca_pem, ca_form = decode_pem_variable(environ.get("PROBE_TLS_CA_PEM"), "PROBE_TLS_CA_PEM")
    except PemError as exc:
        results.update(status="error", error=str(exc), finished_at=utc_now_iso())
        return results
    results["pinned_certificate"] = {"sha256": pem_sha256_fingerprint(ca_pem), "variable_form": ca_form}
    wait = float(environ.get("PROBE_STARTUP_WAIT_SECONDS", "180")) if wait_seconds is None else wait_seconds
    wait_for_service(candidates, wait)

    hosts = [check_host(c["host"], c["port"], ca_pem) | {"source": c["source"]} for c in candidates]
    results["hosts"] = hosts
    chosen = next((h for h in hosts if h["verified"]), None)
    fallback = next((h for h in hosts if h["connects"]), None) if chosen is None else None
    large_kb = int(environ.get("PROBE_LARGE_BODY_KB", "300"))
    echo: dict[str, Any] = {"transport": None, "cases": []}
    if chosen is not None:
        echo["transport"] = {"host": chosen["host"], "port": chosen["port"], "kind": "verified-tls"}
        context: ssl.SSLContext | None = build_pinned_context(ca_pem)
        echo["cases"] = run_echo_suite(chosen["host"], chosen["port"], context, large_kb=large_kb)
    elif fallback is not None:
        unverified = ssl.create_default_context()
        unverified.minimum_version = ssl.TLSVersion.TLSv1_2
        unverified.check_hostname = False
        unverified.verify_mode = ssl.CERT_NONE  # routing test only; no secret is sent
        echo["transport"] = {"host": fallback["host"], "port": fallback["port"],
                             "kind": "unverified-fallback"}
        echo["cases"] = run_echo_suite(fallback["host"], fallback["port"], unverified,
                                       large_kb=large_kb)
    echo["all_exact"] = bool(echo["cases"]) and all(c.get("exact") for c in echo["cases"])
    results["echo"] = echo
    results["summary"] = {
        "hosts_verified": [f"{h['host']}:{h['port']}" for h in hosts if h["verified"]],
        "hosts_resolving": [f"{h['host']}:{h['port']}" for h in hosts if h["resolves"]],
        "hosts_connecting": [f"{h['host']}:{h['port']}" for h in hosts if h["connects"]],
        "echo_transport": (echo["transport"] or {}).get("kind"),
        "echo_cases": len(echo["cases"]),
        "echo_cases_exact": sum(1 for c in echo["cases"] if c.get("exact")),
        "echo_all_exact": echo["all_exact"],
    }
    results.update(status="done", finished_at=utc_now_iso())
    return results


# ---------------------------------------------------------------------------------- web


def make_handler(state: ProbeState, observer: OrderingObserver, token: str | None) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "probe-client"
        sys_version = ""

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _json(self, status: int, payload: Mapping[str, Any]) -> None:
            data = json.dumps(payload, sort_keys=True, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/health":
                observer.observe("first_health_check")
                self._json(200, {"ok": True})
                return
            observer.observe("first_request")
            if path != "/results":
                self._json(404, {"error": "not found"})
                return
            if not token:
                self._json(503, {"error": "PROBE_RESULTS_TOKEN is not set; results are not served"})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                self._json(401, {"error": "bearer token required"})
                return
            snapshot = state.snapshot()
            snapshot["ordering"] = observer.observations
            self._json(200, snapshot)

        def _refuse(self) -> None:
            self._json(405, {"error": "read-only"})

        do_POST = do_PUT = do_DELETE = do_PATCH = _refuse

    return Handler


def build_gateway(environ: Mapping[str, str]) -> tuple[DbGateway | None, dict[str, Any] | None]:
    url = environ.get("PROBE_DB_URL") or environ.get("DATABASE_URL")
    if not url:
        return None, None
    try:
        info = parse_connection_info(url)
    except ConnectionInfoError as exc:
        return None, {"error": str(exc)}
    return PsycopgGateway(info), info.public()


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    state = ProbeState()
    gateway, db_public = build_gateway(env)
    observer = OrderingObserver(gateway)
    port = int(env.get("PORT", env.get("PROBE_HTTP_PORT", "8080")))
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(  # noqa: S104 - the public route
        state, observer, env.get("PROBE_RESULTS_TOKEN") or None))
    server.daemon_threads = True

    def worker() -> None:
        results = run_probe(env, state)
        results["database_connection_info"] = db_public
        with state.lock:
            state.results.update(results)
        log_json("probe_client_summary", **{k: v for k, v in results.items()
                                           if k in ("status", "summary", "hosts", "echo",
                                                    "pinned_certificate", "candidates", "error",
                                                    "environment_variable_names")})

    threading.Thread(target=worker, daemon=True).start()
    log_json("probe_client_started", port=port, results_token_set=bool(env.get("PROBE_RESULTS_TOKEN")))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
