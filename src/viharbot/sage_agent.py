"""The Sage service: philosophical analysis over private JSON REST.

Sage is a specialist the orchestrator may call; it does not plan, delegate or call other agents.
It receives a `SageRequest` (the question plus a fixed set of other agents' results), asks the model
for a `SageAnalysis` through the externally managed LiteLLM gateway with `build_agent()`, passes the
result through a deterministic guard, and returns the standard specialist envelope.

Configuration: LITELLM_BASE_URL, LITELLM_API_KEY and its own model alias SAGE_MODEL (no default in
code; Compose supplies one). SAGE_MODEL_TIMEOUT_SECONDS bounds the model call.

Limitations: the HTTP interface trusts the private network unless VIHARBOT_SERVICE_AUTH is on, which
authenticates callers of /process (docs/service-authentication.md; authentication only, no grant).
Nothing here calls a real model in automated checks.
"""

from __future__ import annotations

import logging
import os
import time
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Response

from .body_limit import BodySizeLimitMiddleware, max_body_bytes_from_environment
from .contracts import SpecialistResponse
from .memory import remember
from .model_agent import build_agent
from .observability import configure_logging, get_request_id
from .sage import SageRequest, build_sage_prompt, guard_analysis, sage_system_prompt
from .schemas import SageAnalysis
from .service_auth import SAGE_CAPABILITY, authenticator_from_environment, require_assertion

configure_logging()
logger = logging.getLogger("sage")
app = FastAPI(title="ViharBot Sage Agent", version="1.0")

# Service authentication (ADR-006 A2): off unless VIHARBOT_SERVICE_AUTH is set; a missing or empty
# registry then stops startup. /health and /model stay open (follow-up: /model).
_authenticator = authenticator_from_environment("sage")
_auth_dependencies = (
    [Depends(require_assertion(_authenticator, SAGE_CAPABILITY))] if _authenticator else []
)
if _authenticator:
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=max_body_bytes_from_environment(),
        paths=frozenset({"/process"}),
        agent="sage",
    )

MODEL_ENV = "SAGE_MODEL"
TIMEOUT_ENV = "SAGE_MODEL_TIMEOUT_SECONDS"
# Below the orchestrator's 90 s read cap on a REST call, so Sage fails with its own error first.
DEFAULT_TIMEOUT_SECONDS = 80.0
TIMEOUT_BOUNDS = (5.0, 180.0)


def model_timeout() -> float:
    """The model call's timeout: the environment value, clamped; a bad value falls back."""
    low, high = TIMEOUT_BOUNDS
    try:
        value = float(os.getenv(TIMEOUT_ENV, str(DEFAULT_TIMEOUT_SECONDS)))
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return min(high, max(low, value)) if value == value else DEFAULT_TIMEOUT_SECONDS  # nan -> default


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "agent": "sage"}


@app.get("/model")
def model() -> dict[str, str | None]:
    """The configured model alias, so the web UI can show who uses what. Not a secret."""
    return {"agent": "sage", "model_alias": os.getenv(MODEL_ENV, "").strip() or None}


@app.post("/process", response_model=SpecialistResponse, dependencies=_auth_dependencies)
def process(
    body: SageRequest,
    response: Response,
    x_request_id: str | None = Header(default=None),
) -> SpecialistResponse:
    request_id = get_request_id(x_request_id) if x_request_id else str(uuid4())
    response.headers["X-Request-ID"] = request_id
    model_alias = os.getenv(MODEL_ENV, "").strip()
    started = time.perf_counter()

    def elapsed_ms() -> float:
        return round((time.perf_counter() - started) * 1000, 2)

    try:
        # No retries: LiteLLM's client retries twice by default, which would multiply the timeout.
        agent = build_agent(
            model_alias=model_alias,
            system_prompt=sage_system_prompt(),
            timeout=model_timeout(),
            max_retries=0,
        )
        outcome = agent(build_sage_prompt(body), structured_output_model=SageAnalysis)
        analysis = outcome.structured_output
        if not isinstance(analysis, SageAnalysis):
            raise ValueError("the model did not return a SageAnalysis")
        analysis = guard_analysis(analysis, body.context.supplied())
        serialized = analysis.model_dump()
        remember("sage", request_id, serialized)
        # Token counts are logged only when the client reports them; nothing is estimated.
        usage = getattr(getattr(outcome, "metrics", None), "accumulated_usage", None)
        extra = {"agent": "sage", "request_id": request_id, "model_alias": model_alias or None,
                 "duration_ms": elapsed_ms()}
        if isinstance(usage, dict):
            extra.update(input_tokens=int(usage.get("inputTokens") or 0),
                         output_tokens=int(usage.get("outputTokens") or 0))
        logger.info("sage completed", extra=extra)
        return SpecialistResponse(
            request_id=request_id,
            agent="sage",
            requested_model_alias=model_alias,
            result=serialized,
        )
    except Exception as exc:
        # The error type is logged; its message may echo model or request text, so it is not.
        logger.error(
            "sage failed",
            extra={"agent": "sage", "request_id": request_id, "model_alias": model_alias or None,
                   "duration_ms": elapsed_ms(), "error": type(exc).__name__},
        )
        raise HTTPException(
            status_code=502,
            detail="sage processing failed",
            headers={"X-Request-ID": request_id},
        ) from None
