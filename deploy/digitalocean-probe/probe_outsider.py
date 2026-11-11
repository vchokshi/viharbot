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
No secrets are used or printed. Outbound calls go only to the configured targets and URLs.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import sys
import time
from typing import Any, Mapping
from urllib.parse import urlsplit

from probe_client import parse_host_port
from probe_common import log_json, scrub

HEALTH_PATH = "/__probe/health"


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
    return {
        "probe": "outsider", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targets": targets, "public_urls": urls,
        "summary": {
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
