"""The memory service HTTP application (plain JSON REST over FastAPI).

Order of decisions on every ``/memory/...`` request, each refusing before the next runs:

1. ADR-006 A2 assertion (always on here; there is no off switch). Missing, malformed or unverified
   is 401. The route capability is ``memory.read`` for GET and ``memory.write`` for POST.
2. The raw path must be canonical and name a served scope (400, or 501 for the reserved tiers).
3. The policy table must name the verified ``sub``, and the path scope must be that agent's own
   (403 with one generic body whatever the reason).
4. Query parameters and the body are validated (400 / 422). A scope, owner, agent or engagement
   in either is refused: scope is only ever in the path.
5. The repository applies the stored state's rules.

Errors are short fixed bodies and never echo input. ``/health`` is the only open route.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from typing import Callable, Mapping

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..body_limit import BodySizeLimitMiddleware, max_body_bytes_from_environment
from ..observability import configure_logging, get_request_id
from ..service_auth import (
    Caller,
    KeyRegistry,
    ServiceAuthConfigError,
    ServiceAuthenticator,
    require_assertion,
)
from .audit import audit
from .models import BODY_MODELS
from .paths import MemoryPath, PathError, ReservedScope, parse_memory_path
from .policy import PolicyProvider
from .postgres import PostgresMemoryRepository
from .repository import MemoryRepository, MemoryStoreError
from .service import (
    DEFAULT_LIST_LIMIT,
    MAX_LIST_LIMIT,
    MemoryService,
    Outcome,
    ServiceError,
)

logger = logging.getLogger("memory")

AUDIENCE = "memory"
READ_CAPABILITY = "memory.read"
WRITE_CAPABILITY = "memory.write"
_TAG_RE = r"^[a-z0-9][a-z0-9._-]{0,31}$"


class _PrefixPaths:
    """``BodySizeLimitMiddleware`` asks ``path in paths``; every memory path is capped."""

    def __contains__(self, path: object) -> bool:
        return isinstance(path, str) and path.startswith("/memory/")


class _Audited(Exception):
    """A refusal carried out of the worker thread together with what the audit line needs."""

    def __init__(self, error: ServiceError, scope: str, operation: str, memory_id: str | None) -> None:
        super().__init__(error.audit_reason)
        self.error, self.scope, self.operation, self.memory_id = error, scope, operation, memory_id


def _operation(method: str, path: MemoryPath) -> str | None:
    if method == "GET" and path.action is None:
        return "read" if path.memory_id else "list"
    if method == "POST":
        if path.memory_id is None:
            return "create"
        if path.action:
            return path.action
    return None


def _json(status: int, body: dict, request_id: str, extra: Mapping[str, str] | None = None) -> JSONResponse:
    headers = {"X-Request-ID": request_id, "Cache-Control": "no-store", **(extra or {})}
    return JSONResponse(body, status_code=status, headers=headers)


def create_app(
    *,
    repository: MemoryRepository,
    authenticator: ServiceAuthenticator,
    policies: PolicyProvider,
    clock: Callable[[], datetime] | None = None,
    max_body_bytes: int | None = None,
) -> FastAPI:
    service = MemoryService(repository, policies, **({"clock": clock} if clock else {}))
    app = FastAPI(title="ViharBot Memory Service", version="1.0", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=max_body_bytes or max_body_bytes_from_environment(),
        paths=_PrefixPaths(),
        agent="memory",
    )
    read_dependency = require_assertion(authenticator, READ_CAPABILITY)
    write_dependency = require_assertion(authenticator, WRITE_CAPABILITY)

    def _scope_label_for_audit(request: Request) -> str:
        try:
            return parse_memory_path(request.scope.get("raw_path") or b"").scope_label
        except PathError:
            return "unparsed"

    async def authenticate(request: Request) -> Caller:
        dependency = read_dependency if request.method == "GET" else write_dependency
        request.state.request_id = get_request_id(request.headers.get("x-request-id"))
        try:
            return await dependency(request)
        except HTTPException as error:
            # service_auth has logged the decision; this is the memory-level audit of the denial.
            audit(
                request_id=request.state.request_id,
                caller=None,
                scope=_scope_label_for_audit(request),
                operation=request.method.lower(),
                outcome="denied",
                reason="authentication_failed",
            )
            raise error

    @app.get("/health")
    def health() -> JSONResponse:
        try:
            repository.ping()
        except MemoryStoreError:
            return JSONResponse({"status": "unavailable", "agent": "memory"}, status_code=503)
        return JSONResponse({"status": "ok", "agent": "memory"})

    def _query(request: Request, operation: str) -> dict:
        """Only ``tag`` and ``limit`` on a list. Anything else, such as a scope, is refused."""
        allowed = {"tag", "limit"} if operation == "list" else set()
        params = request.query_params.multi_items()
        names = [name for name, _ in params]
        if len(set(names)) != len(names) or any(name not in allowed for name in names):
            raise ServiceError(400, "bad request", "unexpected_query_parameter")
        found = dict(params)
        if operation != "list":
            return {}
        tag = found.get("tag")
        if tag is not None:
            if not re.fullmatch(_TAG_RE, tag):
                raise ServiceError(400, "bad request", "bad_tag")
        limit = DEFAULT_LIST_LIMIT
        if "limit" in found:
            text = found["limit"]
            if not (text.isascii() and text.isdigit() and 1 <= int(text) <= MAX_LIST_LIMIT):
                raise ServiceError(400, "bad request", "bad_limit")
            limit = int(text)
        return {"tag": tag, "limit": limit}

    def _handle(request: Request, caller: Caller, body_bytes: bytes, request_id: str) -> tuple[Outcome, str, str, str | None]:
        """Runs in a worker thread. Returns the outcome, scope label, operation and memory id."""
        scope_label, operation, memory_id = "unparsed", request.method.lower(), None
        try:
            path = parse_memory_path(request.scope.get("raw_path") or b"")
            scope_label, memory_id = path.scope_label, path.memory_id
            operation = _operation(request.method, path) or operation
            if _operation(request.method, path) is None:
                raise ServiceError(404, "not found", "no_such_route")
            policy = service.authorize(caller.sub, path, "read" if operation == "list" else operation)
            args = _query(request, operation)
            model = BODY_MODELS.get(operation)
            parsed = None
            if model is not None:
                try:
                    parsed = model.model_validate_json(body_bytes if body_bytes else b"{}")
                except ValidationError:
                    raise ServiceError(422, "invalid request", "invalid_body", "invalid_body") from None
            elif body_bytes:
                raise ServiceError(400, "bad request", "unexpected_body")
            if operation == "list":
                outcome = service.list(policy, path, args["tag"], args["limit"])
            elif operation == "read":
                outcome = service.get(policy, path, path.memory_id)
            elif operation == "create":
                outcome = service.create(caller.sub, policy, path, parsed, request_id)
            else:
                outcome = getattr(service, operation)(caller.sub, policy, path, path.memory_id, parsed, request_id)
            return outcome, scope_label, operation, outcome.memory_id
        except ReservedScope:
            raise _Audited(ServiceError(501, "not implemented", "reserved_scope"), "reserved", operation, None) from None
        except PathError as error:
            raise _Audited(ServiceError(400, "bad request", error.reason), scope_label, operation, None) from None
        except ServiceError as error:
            raise _Audited(error, scope_label, operation, memory_id) from None
        except Exception as exc:  # a bug: refuse generically, log the class only, never the message
            logger.error("unexpected memory service error", extra={"agent": "memory", "error": type(exc).__name__})
            raise _Audited(ServiceError(500, "internal error", "unexpected_error"), scope_label, operation, memory_id) from None

    @app.api_route("/memory/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def memory_route(request: Request, caller: Caller = Depends(authenticate)) -> JSONResponse:
        request_id = request.state.request_id
        body_bytes = await request.body()
        try:
            outcome, scope_label, operation, memory_id = await run_in_threadpool(
                _handle, request, caller, body_bytes, request_id
            )
        except _Audited as refused:
            error = refused.error
            outcome_name = {400: "invalid", 404: "not_found", 409: "conflict", 422: "invalid",
                            403: "denied", 501: "denied", 503: "error", 500: "error"}.get(error.status, "error")
            audit(
                request_id=request_id, caller=caller.sub, scope=refused.scope, operation=refused.operation,
                outcome=outcome_name, reason=error.audit_reason, memory_id=refused.memory_id,
            )
            return _json(error.status, error.body(), request_id)
        audit(
            request_id=request_id, caller=caller.sub, scope=scope_label, operation=operation,
            outcome="replayed" if outcome.replayed else "ok", memory_id=memory_id,
        )
        return _json(outcome.status, outcome.payload, request_id, outcome.headers)

    app.state.service = service
    return app


def _is_loopback_host(host: str) -> bool:
    import ipaddress

    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def conninfo_from_environment(
    environ: Mapping[str, str],
    prefix: str = "MEMORY_DB_",
    *,
    sslrootcert: str | None = None,
    strict_tls: bool = False,
) -> str:
    """A libpq connection string from MEMORY_DB_* settings. Nothing has a default but the port.

    The password is only ever read from the environment (or a mounted secret file) and is never
    logged. The service role and the migration role use the same names with different prefixes.

    ``strict_tls`` is for the Stage launcher only (``stage_entry``, ``MEMORY_STAGE_ROLE=serve``);
    Compose never sets it, so its unencrypted private-network connection is unchanged. With it the
    mode is forced to ``verify-full`` whatever ``*_SSLMODE`` says, ``sslrootcert`` (a file path
    written by the launcher) is passed to libpq, and a host that is not loopback is refused unless
    a CA file was given, because ``verify-full`` cannot be satisfied without one. A loopback host
    without a CA keeps the configured mode (local development only).
    """
    from psycopg.conninfo import make_conninfo

    def need(name: str) -> str:
        value = environ.get(prefix + name, "").strip()
        if not value:
            raise ServiceAuthConfigError(f"{prefix}{name} is required")
        return value

    host = need("HOST")
    sslmode = environ.get(prefix + "SSLMODE", "prefer").strip() or "prefer"
    extra: dict[str, str] = {}
    if strict_tls:
        if sslrootcert:
            sslmode = "verify-full"
            extra["sslrootcert"] = sslrootcert
        elif not _is_loopback_host(host):
            raise ServiceAuthConfigError(
                f"{prefix}HOST is not loopback, so sslmode verify-full with a CA file is required "
                "(set MEMORY_DB_CA_PEM)"
            )
    return make_conninfo(
        host=host,
        port=environ.get(prefix + "PORT", "5432").strip() or "5432",
        dbname=need("NAME"),
        user=need("USER"),
        password=need("PASSWORD"),
        sslmode=sslmode,
        **extra,
    )


def create_app_from_environment(environ: Mapping[str, str] | None = None, *, conninfo: str | None = None) -> FastAPI:
    """Production entry point (``uvicorn --factory``). Refuses to start without a key registry.

    ``conninfo`` is passed only by the Stage launcher, which has already built a strict TLS
    connection string; the ``uvicorn --factory`` path (Compose) never passes it.
    """
    env = os.environ if environ is None else environ
    configure_logging()
    registry_path = env.get("VIHARBOT_KEY_REGISTRY", "").strip()
    if not registry_path:
        raise ServiceAuthConfigError("VIHARBOT_KEY_REGISTRY is required: the memory service has no unauthenticated mode")
    registry = KeyRegistry(registry_path)
    registry.load_initial()
    authenticator = ServiceAuthenticator(audience=AUDIENCE, registry=registry)
    policy_file = env.get("MEMORY_POLICY_FILE", "").strip()
    policies = PolicyProvider.from_file(policy_file) if policy_file else PolicyProvider.from_file()
    repository = PostgresMemoryRepository(conninfo if conninfo is not None else conninfo_from_environment(env))
    app = create_app(repository=repository, authenticator=authenticator, policies=policies)
    try:
        finished = app.state.service.complete_pending_erasures()
        if finished:
            logger.info("finished pending erasures", extra={"agent": "memory", "completed": finished})
    except MemoryStoreError:
        logger.warning("could not check pending erasures at start", extra={"agent": "memory"})
    return app
