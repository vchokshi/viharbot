"""probe-service: the echo and TLS service (RUNBOOK facts 2 and 3). THROWAWAY.

It serves HTTPS on an internal port with a self-signed certificate that arrives as encrypted
environment variables, and echoes back what it actually received so a second component can compare
sent against received:

  * the HTTP method,
  * the raw request target exactly as it appeared on the request line (path and query, percent
    encoding and its case, repeated and ordered query keys, a trailing empty query),
  * header NAMES as received (case and order kept), plus the values of a few safe headers
    (content-type, content-length, host, transfer-encoding),
  * the SHA-256 and length of the body bytes (content-length or chunked framing).

Two reserved paths: GET /__probe/health and GET /__probe/info (service identity and the public
certificate fingerprint). Everything else is echoed. It is read-only, holds no state, makes no
outbound calls, and never prints or returns the certificate private key, headers' secret values or
request bodies.

Environment: PROBE_TLS_CERT_PEM and PROBE_TLS_KEY_PEM (PEM, escaped-newline PEM or base64 of PEM),
PROBE_LISTEN_HOST (default 0.0.0.0), PROBE_LISTEN_PORT (default 8443), PROBE_MAX_BODY_BYTES.
The service refuses to start without a usable certificate and key.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping

from probe_common import (
    PemError,
    TempSecretFiles,
    decode_pem_variable,
    fail_hard,
    log_json,
    pem_sha256_fingerprint,
    utc_now_iso,
)

SAFE_HEADERS = ("content-type", "content-length", "host", "transfer-encoding")
RESERVED_PREFIX = "/__probe/"
DEFAULT_MAX_BODY = 8 * 1024 * 1024


class MissingCertificateError(RuntimeError):
    """No usable certificate and key were supplied. The message names variables, never values."""


def load_tls_material(environ: Mapping[str, str]) -> tuple[str, str, dict[str, str]]:
    """Return (certificate PEM, key PEM, {variable: form}) or raise MissingCertificateError."""
    try:
        cert, cert_form = decode_pem_variable(environ.get("PROBE_TLS_CERT_PEM"), "PROBE_TLS_CERT_PEM")
        key, key_form = decode_pem_variable(environ.get("PROBE_TLS_KEY_PEM"), "PROBE_TLS_KEY_PEM")
    except PemError as exc:
        raise MissingCertificateError(str(exc)) from None
    if "-----BEGIN CERTIFICATE-----" not in cert:
        raise MissingCertificateError("PROBE_TLS_CERT_PEM does not contain a certificate")
    if "PRIVATE KEY-----" not in key:
        raise MissingCertificateError("PROBE_TLS_KEY_PEM does not contain a private key")
    return cert, key, {"PROBE_TLS_CERT_PEM": cert_form, "PROBE_TLS_KEY_PEM": key_form}


def build_server_context(cert_pem: str, key_pem: str) -> ssl.SSLContext:
    """Load the certificate through 0600 temporary files that are removed as soon as it is loaded."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with TempSecretFiles({"server.crt": cert_pem, "server.key": key_pem}) as paths:
        context.load_cert_chain(str(paths["server.crt"]), str(paths["server.key"]))
    return context


def read_body(handler: BaseHTTPRequestHandler, limit: int) -> tuple[bytes, str]:
    """Read the request body (content-length or chunked). Returns (bytes, framing)."""
    encoding = (handler.headers.get("Transfer-Encoding") or "").lower()
    if "chunked" in encoding:
        chunks: list[bytes] = []
        total = 0
        while True:
            size_line = handler.rfile.readline(64).strip()
            size = int(size_line.split(b";")[0] or b"0", 16)
            if size == 0:
                while handler.rfile.readline(1024).strip():  # trailers, then the blank line
                    pass
                break
            total += size
            if total > limit:
                raise ValueError("body too large")
            chunks.append(handler.rfile.read(size))
            handler.rfile.readline(4)  # CRLF after each chunk
        return b"".join(chunks), "chunked"
    length = handler.headers.get("Content-Length")
    if length is None:
        return b"", "none"
    count = int(length)
    if count > limit:
        raise ValueError("body too large")
    return handler.rfile.read(count) if count else b"", "content-length"


def parse_request_line(raw: bytes) -> tuple[str, str, str]:
    """Split the request line exactly as received: (method, raw target, version).

    `http.server` exposes a `path` it may normalize (newer Python collapses a leading `//`), so the
    echo uses the raw line instead.
    """
    line = raw.decode("latin-1").rstrip("\r\n")
    method, _, rest = line.partition(" ")
    target, _, version = rest.rpartition(" ")
    if not target:  # HTTP/0.9 style or malformed: report what is there
        target, version = rest, ""
    return method, target, version


def echo_payload(raw_request_line: bytes, headers: list[tuple[str, str]], body: bytes,
                 framing: str) -> dict[str, Any]:
    method, target, version = parse_request_line(raw_request_line)
    safe = {name.lower(): value for name, value in headers if name.lower() in SAFE_HEADERS}
    return {
        "probe": "echo",
        "method": method,
        "raw_target": target,
        "http_version": version,
        "header_names": [name for name, _ in headers],
        "safe_headers": safe,
        "body_framing": framing,
        "body_length": len(body),
        "body_sha256": hashlib.sha256(body).hexdigest(),
    }


class ProbeHandler(BaseHTTPRequestHandler):
    server_version = "probe-service"
    sys_version = ""
    # Class attributes set by `make_server`.
    cert_sha256: str = ""
    started_at: str = ""
    max_body: int = DEFAULT_MAX_BODY

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - no per-request logging
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _handle(self) -> None:
        _, target, _ = parse_request_line(self.raw_requestline)
        if target.startswith(RESERVED_PREFIX) and self.command == "GET":
            if target == RESERVED_PREFIX + "health":
                self._send_json(200, {"ok": True})
            elif target == RESERVED_PREFIX + "info":
                self._send_json(200, {
                    "probe": "service", "started_at": self.started_at,
                    "python": platform.python_version(), "container_hostname": socket.gethostname(),
                    "cert_sha256": self.cert_sha256,
                })
            else:
                self._send_json(404, {"error": "unknown reserved path"})
            return
        try:
            body, framing = read_body(self, self.max_body)
        except ValueError:
            self._send_json(413, {"error": "body too large or malformed framing"})
            return
        self._send_json(200, echo_payload(self.raw_requestline, list(self.headers.items()), body,
                                          framing))

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle


def make_server(host: str, port: int, context: ssl.SSLContext | None, *, cert_sha256: str = "",
                max_body: int = DEFAULT_MAX_BODY) -> ThreadingHTTPServer:
    """Build the server. `context=None` serves plain HTTP (used only by offline tests)."""
    handler = type("BoundProbeHandler", (ProbeHandler,), {
        "cert_sha256": cert_sha256, "started_at": utc_now_iso(), "max_body": max_body})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    if context is not None:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    return server


def main(environ: Mapping[str, str] | None = None) -> int:
    env = os.environ if environ is None else environ
    try:
        cert, key, forms = load_tls_material(env)
        context = build_server_context(cert, key)
    except MissingCertificateError as exc:
        return fail_hard(f"probe-service: refusing to start without a certificate: {exc}")
    except (ssl.SSLError, ValueError, OSError):
        return fail_hard("probe-service: the certificate and key could not be loaded together "
                         "(details withheld); check that they match")
    host = env.get("PROBE_LISTEN_HOST", "0.0.0.0")  # noqa: S104 - an internal container port
    port = int(env.get("PROBE_LISTEN_PORT", "8443"))
    max_body = int(env.get("PROBE_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY)))
    fingerprint = pem_sha256_fingerprint(cert)
    server = make_server(host, port, context, cert_sha256=fingerprint, max_body=max_body)
    log_json("probe_service_started", port=port, cert_sha256=fingerprint, variable_forms=forms,
             cert_pem_chars=len(env.get("PROBE_TLS_CERT_PEM", "")),
             key_pem_chars=len(env.get("PROBE_TLS_KEY_PEM", "")))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
