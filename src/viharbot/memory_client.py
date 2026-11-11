"""Client for the governed memory service (ADR-009, issue #40, PR 2). Used by Charlie only.

Memory is an enrichment, never a dependency. Failures are raised as ``MemoryClientError`` with a
fixed, content-free reason code, and the caller proceeds without memory.

Rules enforced here:

* Off unless ``VIHARBOT_MEMORY_ENABLED`` is on. Off means no object, no I/O, no log line.
* Transport (ADR-009): ``https://`` is accepted for any host (verified against the CA in
  ``VIHARBOT_MEMORY_CA_FILE`` when set, otherwise the system trust store). ``http://`` is accepted
  only for a loopback host AND only with ``VIHARBOT_MEMORY_LOCAL_DEV`` on. A plain-HTTP URL to any
  other host, including a private IP, refuses to start the client. Redirects are never followed and
  environment proxies are never used, so a token cannot reach another origin.
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
import ssl
import time
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .outbound_auth import SUBJECT_ENV, OutboundAuthError, outbound_headers

logger = logging.getLogger("memory_client")

ENABLE_ENV = "VIHARBOT_MEMORY_ENABLED"
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
_OFF = ("", "0", "off", "false")
_ON = ("1", "on", "true")


class MemoryClientError(Exception):
    """A memory call did not complete. ``reason`` is a fixed code that is safe to log."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any):  # a redirect is a failure, never followed
        return None


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
        self._base = base_url
        self._timeout = timeout
        handlers: list[Any] = [ProxyHandler({}), HTTPHandler(), _NoRedirect()]
        if ssl_context is not None:
            handlers.append(HTTPSHandler(context=ssl_context))
        self._opener = build_opener(*handlers)

    def _call(self, method: str, target: str, payload: dict | None, request_id: str, agent: str) -> tuple[int, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8") if payload is not None else b""
        url = self._base + target
        try:
            signed = outbound_headers(
                url, destination="memory", method=method, body=body, request_id=request_id,
                default_subject=SUBJECT, agent=agent,
            )
        except OutboundAuthError:
            raise MemoryClientError("signing_failed") from None
        if not signed:  # outbound signing is off: an unsigned call is never made
            raise MemoryClientError("signing_unavailable")
        headers = {"X-Request-ID": request_id, "Accept": "application/json", **signed}
        if payload is not None:
            headers["Content-Type"] = "application/json"
        request = Request(url, data=body if payload is not None else None, headers=headers, method=method)
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                status = response.status
        except HTTPError as exc:
            exc.close()
            raise MemoryClientError("denied" if exc.code in (401, 403) else f"http_{exc.code}") from None
        except TimeoutError:
            raise MemoryClientError("timeout") from None
        except ssl.SSLError:
            raise MemoryClientError("tls_verification_failed") from None
        except URLError as exc:
            if isinstance(exc.reason, ssl.SSLError):
                raise MemoryClientError("tls_verification_failed") from None
            if isinstance(exc.reason, TimeoutError):
                raise MemoryClientError("timeout") from None
            raise MemoryClientError("unreachable") from None
        except http.client.HTTPException:
            raise MemoryClientError("malformed_response") from None
        except OSError:
            raise MemoryClientError("unreachable") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MemoryClientError("response_too_large")
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise MemoryClientError("malformed_response") from None

    def recent(self, request_id: str, *, agent: str = AGENT_ID) -> list[MemoryRecord]:
        """At most MAX_RECORDS of Charlie's own newest metadata records. Raises MemoryClientError."""
        status, data = self._call("GET", f"{RECORDS_PATH}?tag={TAG}&limit={MAX_RECORDS}", None, request_id, agent)
        if status != 200:
            raise MemoryClientError("malformed_response")
        return parse_records(data)

    def write(self, content: str, request_id: str, *, agent: str = AGENT_ID) -> str:
        """Create one record; returns "stored". The idempotency key is derived from the request id,
        so the one retry (or a later replay of the same request) never creates a second record."""
        payload = {
            "idempotency_key": idempotency_key(request_id),
            "content": content,
            "provenance": {"kind": "agent_memory", "source_id": f"synthesis:{request_id}"[:256]},
            "classification": "internal",
            "tags": [TAG],
        }
        last = MemoryClientError("unreachable")
        for _ in range(WRITE_ATTEMPTS):
            try:
                status, data = self._call("POST", RECORDS_PATH, payload, request_id, agent)
            except MemoryClientError as error:
                last = error
                if error.reason in ("timeout", "unreachable") or error.reason.startswith("http_5"):
                    continue  # transient: retry once with the same key
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
    for item in records[:MAX_RECORDS]:
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
        out.append(MemoryRecord(item["memory_id"], item["created_at"], content["text"]))
    return out


def client_from_environment(environ: Mapping[str, str] | None = None) -> MemoryClient | None:
    """None when memory is off (the default). Raises MemoryClientError when it is on but refused."""
    env = os.environ if environ is None else environ
    setting = env.get(ENABLE_ENV, "").strip().lower()
    if setting in _OFF:
        return None
    if setting not in _ON:
        raise MemoryClientError("bad_setting")
    local_dev = env.get(LOCAL_DEV_ENV, "").strip().lower() in _ON
    base = validate_url(env.get(URL_ENV, ""), local_dev=local_dev)
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
