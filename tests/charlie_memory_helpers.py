"""Helpers for the Charlie memory tests (ADR-009, issue #40, PR 2). Offline: no model, no gateway.

* ``FakeRepo``: an in-memory MemoryRepository (database-free) with PostgreSQL-like idempotency.
* ``serve``: runs a real FastAPI app on loopback (http, or https with a generated development CA).
* ``FakeMemoryHttp``: a scripted loopback HTTP server for failure modes.
* ``charlie``: imports the real synthesis service with a stubbed model and returns what it saw.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import logging
import socket
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from viharbot import service_auth as sa
from viharbot.memory_service.repository import (
    ConflictError,
    GuardRefusedError,
    MemoryRepository,
    MutationResult,
    Record,
)
from viharbot.observability import JsonFormatter
from viharbot.schemas import FinalSynthesis

ROOT = Path(__file__).resolve().parents[1]


class FakeRepo(MemoryRepository):
    """Only what Charlie uses: create (with idempotency), list and get. Newest first."""

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.keys: dict[tuple[str, str], tuple[str, MutationResult]] = {}
        self.lock = threading.Lock()

    def ping(self) -> None:
        return None

    def get(self, scope, owner, memory_id, now, allowed_classes):
        with self.lock:
            return next((r for r in self.records if r.memory_id == memory_id and r.owner_agent_id == owner), None)

    def list(self, scope, owner, now, allowed_classes, tag, limit):
        with self.lock:
            found = [
                r for r in reversed(self.records)
                if r.owner_agent_id == owner and r.scope_key == scope.key and r.memory_type == scope.memory_type
                and r.classification in allowed_classes and (tag is None or tag in r.tags)
            ]
            return found[:limit]

    def create(self, new, ctx):
        with self.lock:
            if not ctx.guard():
                raise GuardRefusedError()
            if ctx.idempotency_key:
                seen = self.keys.get((ctx.owner, ctx.idempotency_key))
                if seen:
                    if seen[0] != ctx.request_hash:
                        raise ConflictError("idempotency_conflict")
                    return MutationResult(seen[1].status, seen[1].body, replayed=True)
            record = Record(
                memory_id=new.memory_id, memory_type=ctx.scope.memory_type, scope_key=ctx.scope.key,
                owner_agent_id=ctx.owner, content=new.content, provenance=new.provenance,
                classification=new.classification, tags=new.tags, personal_data=new.personal_data,
                consent_basis=new.consent_basis, consent_reference=new.consent_reference,
                retention_until=new.retention_until, expires_at=new.expires_at,
                created_at=ctx.now + timedelta(milliseconds=len(self.records)), updated_at=ctx.now,
                version=1, rev=1, lifecycle="active",
            )
            self.records.append(record)
            result = MutationResult(201, {"memory_id": new.memory_id, "state": "created", "rev": 1})
            if ctx.idempotency_key:
                self.keys[(ctx.owner, ctx.idempotency_key)] = (ctx.request_hash, result)
            return result

    def _unused(self, *args: Any, **kwargs: Any):
        raise AssertionError("not used by Charlie")

    update = classify = retire = erase = complete_erasure = pending_erasures = _unused


def free_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


@contextlib.contextmanager
def serve(app, *, certfile: str | None = None, keyfile: str | None = None):
    """The real app on 127.0.0.1. Yields the base URL (https uses the name ``localhost``)."""
    import uvicorn

    port = free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
        ssl_certfile=certfile, ssl_keyfile=keyfile,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    assert server.started, "the loopback server did not start"
    try:
        yield f"{'https' if certfile else 'http'}://{'localhost' if certfile else '127.0.0.1'}:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@dataclass
class Seen:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


class FakeMemoryHttp:
    """A scripted loopback server. ``status``, ``body``, ``delay`` and ``headers`` shape every reply."""

    def __init__(self, status: int = 200, body: bytes = b"{}", delay: float = 0.0,
                 headers: dict[str, str] | None = None) -> None:
        self.status, self.body, self.delay, self.extra = status, body, delay, headers or {}
        self.seen: list[Seen] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                outer.seen.append(Seen(self.command, self.path, dict(self.headers), self.rfile.read(length)))
                time.sleep(outer.delay)
                try:
                    self.send_response(outer.status)
                    for name, value in outer.extra.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(outer.body)))
                    self.end_headers()
                    self.wfile.write(outer.body)
                except OSError:
                    pass  # the client gave up (a timeout test)

            do_GET = do_POST = _reply

            def log_message(self, *args: Any) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "FakeMemoryHttp":
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.server.shutdown()
        self.server.server_close()


class LogCapture(logging.Handler):
    """Attached to the named loggers directly: the services reset the root handlers on import."""

    NAMES = ("memory_client", "charlie_memory", "outbound_auth", "specialist", "memory")

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def __enter__(self) -> "LogCapture":
        for name in self.NAMES:
            logging.getLogger(name).addHandler(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        for name in self.NAMES:
            logging.getLogger(name).removeHandler(self)

    def lines(self) -> list[str]:
        return [self.format(r) for r in self.records]

    def everything(self) -> str:
        """Every formatted line and every raw attribute of every record, for leak checks."""
        raw = [json.dumps(r.__dict__, default=str) for r in self.records]
        return "\n".join(self.lines() + raw)

    def memory_events(self, operation: str | None = None) -> list[dict[str, Any]]:
        out = []
        for r in self.records:
            if r.name == "memory_client" and (operation is None or getattr(r, "operation", None) == operation):
                out.append({k: getattr(r, k, None) for k in ("operation", "decision", "reason", "request_id")})
        return out


@dataclass
class Charlie:
    module: Any
    client: TestClient
    calls: list[dict[str, Any]] = field(default_factory=list)
    build_kwargs: list[dict[str, Any]] = field(default_factory=list)

    def ask(self, question: str = "How should we proceed?", context: dict | None = None,
            request_id: str | None = None):
        headers = {"X-Request-ID": request_id} if request_id else {}
        body = {"question": question, "context": context if context is not None else {"research": {"claim": "c"}}}
        return self.client.post("/process", json=body, headers=headers)


def standard_result(answer: str = "A normal answer.") -> FinalSynthesis:
    return FinalSynthesis(answer=answer, key_points=["one", "two"], confidence="medium", caveats=["x"])


def charlie(monkeypatch, tmp_path: Path, env: dict[str, str] | None = None, *,
            result: FinalSynthesis | None = None, agent_kind: str = "synthesis") -> Charlie:
    """The real synthesis service module, imported fresh with ``env``, and a stub in place of the model."""
    for name in ("VIHARBOT_SERVICE_AUTH", "VIHARBOT_KEY_REGISTRY", "VIHARBOT_MEMORY_ENABLED", "VIHARBOT_MEMORY_URL",
                 "VIHARBOT_MEMORY_CA_FILE", "VIHARBOT_MEMORY_LOCAL_DEV", "VIHARBOT_MEMORY_TIMEOUT_SECONDS",
                 "VIHARBOT_OUTBOUND_AUTH", "VIHARBOT_SIGNING_KID", "VIHARBOT_SERVICE_SUBJECT",
                 "VIHARBOT_SIGNING_KEY_FILE", "VIHARBOT_SIGNING_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MEMORY_DIR", str(tmp_path / "jsonl"))
    monkeypatch.setenv("AGENT_KIND", agent_kind)
    monkeypatch.setenv("SYNTHESIS_MODEL", "svc/alias")
    monkeypatch.setenv("FACT_CHECK_MODEL", "svc/alias")
    for name, value in (env or {}).items():
        monkeypatch.setenv(name, value)
    sys.modules.pop("viharbot.specialist_agent", None)
    module = importlib.import_module("viharbot.specialist_agent")
    seen = Charlie(module, TestClient(module.app))
    answer = result or standard_result()

    def build_agent(**kwargs: Any):
        seen.build_kwargs.append(kwargs)

        def run(prompt: str, structured_output_model=None):
            seen.calls.append({"system": kwargs.get("system_prompt"), "user": prompt})
            return types.SimpleNamespace(structured_output=answer)

        return run

    monkeypatch.setattr(module, "build_agent", build_agent)
    return seen


def memory_env(url: str, key_file: str, kid: str = "charlie-test", **extra: str) -> dict[str, str]:
    """Charlie's settings for a loopback http memory service (local development configuration)."""
    env = {
        "VIHARBOT_MEMORY_ENABLED": "on", "VIHARBOT_MEMORY_URL": url, "VIHARBOT_MEMORY_LOCAL_DEV": "on",
        "VIHARBOT_OUTBOUND_AUTH": "on", "VIHARBOT_SIGNING_KID": kid, "VIHARBOT_SIGNING_KEY_FILE": key_file,
        "VIHARBOT_SERVICE_SUBJECT": "charlie", "VIHARBOT_MEMORY_TIMEOUT_SECONDS": "1.5",
    }
    env.update(extra)
    return env


def claims(token: str) -> dict[str, Any]:
    return json.loads(sa.b64url_decode(token.split(".")[1]))


def cleanup_module() -> None:
    sys.modules.pop("viharbot.specialist_agent", None)
