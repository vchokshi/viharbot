"""Client for the governed memory service (ADR-009, issue #40, PR 2). Used by Charlie only.

Memory is an enrichment, never a dependency. Failures are raised as ``MemoryClientError`` with a
fixed, content-free reason code, and the caller proceeds without memory.

Rules enforced here:

* There is no on/off flag. The client is active for the synthesis service whenever
  ``VIHARBOT_MEMORY_URL`` is set. With no URL memory is inactive for that run (not an error, logged
  once), which is the case on Stage: it has no memory service and no URL.
* Transport (ADR-009): ``https://`` is accepted for any host (verified against the CA in
  ``VIHARBOT_MEMORY_CA_FILE`` when set, otherwise the system trust store). ``http://`` is accepted
  only for a loopback host AND only with ``VIHARBOT_MEMORY_LOCAL_DEV`` on. A plain-HTTP URL to any
  other host, including a private IP, refuses to start the client. Redirects are never followed and
  environment proxies are never used, so a token cannot reach another origin.
* Time: every call has ONE monotonic deadline (``VIHARBOT_MEMORY_TIMEOUT_SECONDS``, default 1.5 s)
  covering connect, TLS handshake, request and body read. A watchdog closes the socket at the
  deadline, so a server that trickles bytes cannot stretch a call. A write and its single retry share
  one such budget; each connection attempt (several addresses may be tried) and the TLS handshake
  get only the time left, and the deadline is rechecked between steps. Name resolution (one lookup
  of a Compose service name) is not interruptible: the deadline is checked as soon as it returns.
* Auth: every call is signed with this service's own key and subject ``charlie`` through
  ``outbound_auth``. There is no unsigned fallback: if no assertion can be produced the call is not
  made. The subject is pinned to ``charlie``, never the web key's ``web``.
* Scope: the only path used is ``/memory/agent/charlie/records``. It is a constant, not derived from
  configuration, a token or a record.
* Everything returned is untrusted data. Nothing here interprets a record's text.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

from .outbound_auth import SUBJECT_ENV, OutboundAuthError, outbound_headers

logger = logging.getLogger("memory_client")

URL_ENV = "VIHARBOT_MEMORY_URL"
CA_FILE_ENV = "VIHARBOT_MEMORY_CA_FILE"
LOCAL_DEV_ENV = "VIHARBOT_MEMORY_LOCAL_DEV"
TIMEOUT_ENV = "VIHARBOT_MEMORY_TIMEOUT_SECONDS"

AGENT_ID = "charlie"
SUBJECT = "charlie"
RECORDS_PATH = f"/memory/agent/{AGENT_ID}/records"
TAG = "synthesis-meta"
DEFAULT_TIMEOUT_SECONDS = 1.5
MAX_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_RECORDS = 5  # the most Charlie ever reads back
WRITE_ATTEMPTS = 2  # one retry, with the same idempotency key
_ON = ("1", "on", "true")
MEMORY_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
CREATED_AT_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")
MIN_REMAINING = 0.02


class MemoryClientError(Exception):
    """A memory call did not complete. ``reason`` is a fixed code that is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class MemoryRecord:
    """The few fields of a returned record the caller may use. ``text`` is untrusted data."""

    memory_id: str
    created_at: str
    text: str


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def validate_url(url: str, *, local_dev: bool) -> str:
    """The normalized base URL, or MemoryClientError with a fixed reason. Enforces ADR-009."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname
        parts.port  # noqa: B018  (raises ValueError on a bad port)
    except ValueError:
        raise MemoryClientError("bad_url") from None
    if parts.scheme not in ("http", "https") or not host:
        raise MemoryClientError("bad_url")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        raise MemoryClientError("bad_url")
    if parts.scheme == "http" and not (local_dev and _is_loopback(host)):
        # Not TLS: refused for every non-loopback host (private networks included), and for
        # loopback unless this is explicitly a local-development configuration.
        raise MemoryClientError("insecure_transport")
    return f"{parts.scheme}://{parts.netloc}"


class MemoryClient:
    def __init__(self, base_url: str, *, ssl_context: ssl.SSLContext | None, timeout: float) -> None:
        parts = urlsplit(base_url)
        self._https = parts.scheme == "https"
        self._host = parts.hostname or ""
        self._port = parts.port or (443 if self._https else 80)
        self._context = ssl_context
        self._budget = timeout  # the total time one operation (a read, or a write and its retry) may take

    # Seams for tests; the production values are the standard library's.
    _resolve = staticmethod(socket.getaddrinfo)
    _new_socket = staticmethod(socket.socket)

    def _call(self, method: str, target: str, payload: dict | None, request_id: str, agent: str,
              deadline: float) -> tuple[int, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else b""
        url = f"{'https' if self._https else 'http'}://{self._host}:{self._port}{target}"
        try:
            signed = outbound_headers(
                url, destination="memory", method=method, body=body, request_id=request_id,
                default_subject=SUBJECT, agent=agent,
            )
        except OutboundAuthError:
            raise MemoryClientError("signing_failed") from None
        if not signed:  # outbound signing is off: an unsigned call is never made
            raise MemoryClientError("signing_unavailable")
        headers = {"X-Request-ID": request_id, "Accept": "application/json", "Connection": "close", **signed}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        remaining = deadline - time.monotonic()
        if remaining < MIN_REMAINING:
            raise MemoryClientError("timeout")
        connection = http.client.HTTPConnection(self._host, self._port)
        held: list[socket.socket] = []  # every socket this call opened, so the watchdog can reach it
        lock = threading.Lock()
        expired = threading.Event()

        def register(sock: socket.socket) -> None:
            """Make a socket reachable by the watchdog BEFORE any blocking call on it. If the deadline
            already passed (the watchdog ran while nothing was registered), refuse it at once."""
            with lock:
                if expired.is_set():
                    sock.close()
                    raise TimeoutError
                held.append(sock)

        def abort() -> None:  # the deadline passed: close the sockets, which wakes any blocked operation
            with lock:
                expired.set()
                sockets = list(held)
            for sock in sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        def time_left() -> float:
            left = deadline - time.monotonic()
            if left < MIN_REMAINING or expired.is_set():
                raise TimeoutError
            return left

        watchdog = threading.Timer(remaining, abort)
        watchdog.daemon = True
        watchdog.start()
        try:
            # Connect and handshake by hand (not through http.client) so the watchdog can see the
            # socket the whole time: HTTPConnection drops its reference once the response is handed
            # over, which would leave the body read unguarded. Every blocking step gets only what is
            # left of the one deadline, and the deadline is rechecked between steps.
            infos = self._resolve(self._host, self._port, type=socket.SOCK_STREAM)  # not interruptible
            time_left()
            sock = None
            last_error: OSError = OSError("no address")
            for family, kind, proto, _canon, address in infos:  # all addresses share the same budget
                attempt = self._new_socket(family, kind, proto)
                register(attempt)
                try:
                    attempt.settimeout(time_left())
                    attempt.connect(address)
                except OSError as error:  # includes this attempt's timeout: try the next address
                    last_error = error
                    attempt.close()
                    time_left()  # raises TimeoutError when no budget remains
                    continue
                sock = attempt
                break
            if sock is None:
                raise last_error
            time_left()
            # A duplicate descriptor of the same connection: shutting it down at the deadline wakes a
            # blocked handshake, send or read, and stays valid after http.client closes `sock` when
            # it hands the response body over (an SSL socket cannot be duplicated, hence the raw one).
            register(sock.dup())
            if self._https:
                sock.settimeout(time_left())
                sock = self._context.wrap_socket(  # type: ignore[union-attr]
                    sock, server_hostname=self._host, do_handshake_on_connect=False)
                sock.settimeout(time_left())
                sock.do_handshake()  # bounded by the time left, and by the watchdog
            sock.settimeout(time_left())
            connection.sock = sock
            connection.request(method, target, body=body if payload is not None else None, headers=headers)
            response = connection.getresponse()
            status = response.status
            chunks: list[bytes] = []
            size = 0
            while size <= MAX_RESPONSE_BYTES:
                left = deadline - time.monotonic()
                if left < MIN_REMAINING:
                    raise TimeoutError
                chunk = response.read(min(8192, MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            raw = b"".join(chunks)
        except ssl.SSLCertVerificationError:
            raise MemoryClientError("timeout" if time.monotonic() >= deadline else "tls_verification_failed") from None
        except (TimeoutError, ssl.SSLError, http.client.HTTPException, OSError):
            if time.monotonic() >= deadline - 0.001:
                raise MemoryClientError("timeout") from None
            raise MemoryClientError("unreachable") from None
        finally:
            watchdog.cancel()
            for guard in held:
                try:
                    guard.close()
                except OSError:
                    pass
            try:
                connection.close()
            except OSError:
                pass
        if status in (401, 403):
            raise MemoryClientError("denied")
        if status not in (200, 201):
            raise MemoryClientError(f"http_{status}")  # includes redirects, which are never followed
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoryClientError("response_too_large")
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MemoryClientError("malformed_response") from None

    def recent(self, request_id: str, *, agent: str = AGENT_ID) -> list[MemoryRecord]:
        """At most MAX_RECORDS of Charlie's own newest metadata records. Raises MemoryClientError."""
        deadline = time.monotonic() + self._budget
        status, data = self._call(
            "GET", f"{RECORDS_PATH}?tag={TAG}&limit={MAX_RECORDS}", None, request_id, agent, deadline)
        if status != 200:
            raise MemoryClientError("malformed_response")
        return parse_records(data)

    def write(self, content: str, request_id: str, *, agent: str = AGENT_ID) -> str:
        """Create one record; returns "stored". The idempotency key is derived from the request id,
        so the one retry (or a later replay of the same request) never creates a second record.
        The write and its retry share one total budget."""
        payload = {
            "idempotency_key": idempotency_key(request_id),
            "content": content,
            "provenance": {"kind": "agent_memory", "source_id": f"synthesis:{request_id}"[:256]},
            "classification": "internal",
            "tags": [TAG],
        }
        deadline = time.monotonic() + self._budget
        last = MemoryClientError("unreachable")
        for _ in range(WRITE_ATTEMPTS):
            try:
                status, data = self._call("POST", RECORDS_PATH, payload, request_id, agent, deadline)
            except MemoryClientError as error:
                last = error
                if error.reason == "unreachable" or error.reason.startswith("http_5"):
                    continue  # transient: retry once with the same key, if budget remains
                raise
            if status not in (200, 201) or not isinstance(data, dict) or not isinstance(data.get("memory_id"), str):
                raise MemoryClientError("malformed_response")
            return "stored"
        raise last


def idempotency_key(request_id: str) -> str:
    """Deterministic: the same request id always yields the same key (service pattern: 8-128 chars)."""
    return "charlie:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:48]


def parse_records(data: Any) -> list[MemoryRecord]:
    """Validate a list response. Anything unexpected is a malformed response, not a partial one."""
    if not isinstance(data, dict) or data.get("kind") != "memory_record_list" or data.get("not_instructions") is not True:
        raise MemoryClientError("malformed_response")
    records = data.get("records")
    if not isinstance(records, list):
        raise MemoryClientError("malformed_response")
    out: list[MemoryRecord] = []
    for item in records[:MAX_RECORDS]:  # more than MAX_RECORDS are ignored
        try:
            content = item["content"]
            ok = (
                item["owner_agent_id"] == AGENT_ID and item["scope_key"] == AGENT_ID
                and item["memory_type"] == "agent_private" and content["role"] == "data"
                and isinstance(content["text"], str) and isinstance(item["memory_id"], str)
                and isinstance(item["created_at"], str)
            )
        except (KeyError, TypeError):
            ok = False
        if not ok:
            raise MemoryClientError("malformed_response")
        # Both are rendered into the prompt, so they must have exactly the shape the service emits
        # (a lowercase UUID, an ISO-8601 timestamp). A record that does not is dropped, not shown.
        if not MEMORY_ID_RE.fullmatch(item["memory_id"]) or not (
            len(item["created_at"]) <= 40 and CREATED_AT_RE.fullmatch(item["created_at"])
        ):
            continue
        out.append(MemoryRecord(item["memory_id"], item["created_at"], content["text"]))
    return out


def client_from_environment(environ: Mapping[str, str] | None = None) -> MemoryClient | None:
    """None when no memory URL is configured (memory inactive, not an error). Raises
    MemoryClientError when a URL is configured but unusable, so the client stays off."""
    env = os.environ if environ is None else environ
    url = env.get(URL_ENV, "").strip()
    if not url:
        return None
    local_dev = env.get(LOCAL_DEV_ENV, "").strip().lower() in _ON
    base = validate_url(url, local_dev=local_dev)
    subject = env.get(SUBJECT_ENV, "").strip()
    if subject and subject != SUBJECT:  # never the web key's subject
        raise MemoryClientError("wrong_subject")
    context = None
    if base.startswith("https://"):
        try:
            context = ssl.create_default_context(cafile=env.get(CA_FILE_ENV, "").strip() or None)
        except (OSError, ssl.SSLError, ValueError):
            raise MemoryClientError("bad_ca_file") from None
    try:
        timeout = float(env.get(TIMEOUT_ENV, "") or DEFAULT_TIMEOUT_SECONDS)
    except ValueError:
        raise MemoryClientError("bad_timeout") from None
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise MemoryClientError("bad_timeout")
    return MemoryClient(base, ssl_context=context, timeout=timeout)


def log_event(
    agent: str, request_id: str, operation: str, decision: str, reason: str, started: float | None = None
) -> None:
    """One content-free line: identifiers, a decision and a fixed reason code. Nothing else."""
    extra: dict[str, Any] = {
        "agent": agent, "request_id": request_id, "destination_agent": "memory",
        "operation": operation, "decision": decision, "reason": reason,
    }
    if started is not None:
        extra["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
    (logger.info if decision == "ok" else logger.warning)("memory client", extra=extra)
