"""The AgentCore Runtime adapter: `GET /ping` and `POST /invocations` around one poll.

AgentCore authenticates callers (IAM) before a request reaches the container, so this service has
no authentication of its own and must never be exposed beyond AgentCore (a local run binds to
loopback). It is deliberately standalone: it does not import another agent's code, so the steward
stays independently deployable.

Input is a strict schema with two actions: `poll` (what the schedule sends) and `check` for one
pull request number ("check now"). Anything else is refused with 422 before any work. An invalid
configuration stops the service from starting, so a misconfigured container is never "healthy".
The response is counts and codes only: never an email body, a token or contributor text.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, ValidationError, model_validator

from ..body_limit import BodySizeLimitMiddleware
from .config import StewardConfig, from_env
from .poller import PollResult, run_poll

logger = logging.getLogger("kepler")
MAX_BODY_BYTES = 16_384  # an invocation is a few dozen bytes; anything large is not one
_ENVELOPE_KEYS = {"request_id", "input"}


class Invocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["poll", "check"] = "poll"
    pr: StrictInt | None = Field(default=None, ge=1, lt=10**9)

    @model_validator(mode="after")
    def _consistent(self) -> Invocation:
        if self.action == "check" and self.pr is None:
            raise ValueError("check needs a pull request number")
        if self.action == "poll" and self.pr is not None:
            raise ValueError("poll does not take a pull request number")
        return self


Runner = Callable[[Invocation, str | None], PollResult]


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class AwsWiring:
    """Builds the real AWS-backed dependencies on first use, then reuses them across invocations
    (so the installation token is cached). Nothing is created at import or at startup."""

    def __init__(self, config: StewardConfig, clock: Callable[[], datetime.datetime] = _now) -> None:
        self.config, self._clock = config, clock
        self._deps: dict[str, Any] | None = None
        self._build_lock = threading.Lock()

    def deps(self) -> dict[str, Any]:
        with self._build_lock:
            if self._deps is None:
                from . import aws
                from .alerts import SnsPublisher
                from .audit import AuditLog
                from .bedrock import BedrockOpeningModel
                from .github_auth import InstallationTokens, load_secret_from_aws
                from .metrics import CloudWatchMetrics
                from .state import ObservationState

                cfg, region = self.config, self.config.region
                secrets = aws.secrets_client(region)
                dynamo = aws.dynamodb_client(region)
                self._deps = {
                    "token_source": InstallationTokens(
                        lambda: load_secret_from_aws(secrets, cfg.reader_secret_id), cfg.repo,
                        self._clock),
                    "make_client": None,
                    "state": ObservationState(dynamo, cfg.state_table, self._clock,
                                              cfg.dedup_ttl_days, cfg.pending_retry_minutes,
                                              cfg.max_alert_attempts),
                    "audit": AuditLog(dynamo, cfg.audit_table, self._clock),
                    "publisher": SnsPublisher(aws.sns_client(region), cfg.alert_topic_arn),
                    "metrics": CloudWatchMetrics(aws.cloudwatch_client(region)),
                    "clock": self._clock,
                    # Opt-in: with no model id configured the drafts are the plain template.
                    "drafter": (BedrockOpeningModel(aws.bedrock_runtime_client(region), cfg.model_id,
                                                    cfg.model_max_tokens)
                                if cfg.model_id else None),
                }
            return self._deps

    def run(self, invocation: Invocation, request_id: str | None) -> PollResult:
        return run_poll(self.config, request_id=request_id or "scheduled", dry_run=False,
                        only_pr=invocation.pr, **self.deps())

    def note_internal_failure(self) -> None:
        """Best effort: tell the failure alarm about a crash we could not turn into a result."""
        try:
            self.deps()["metrics"].put("PollFailed", 1)
        except Exception:
            pass


def create_app(config: StewardConfig, runner: Runner | None = None,
               on_internal_failure: Callable[[], None] | None = None) -> FastAPI:
    wiring = None
    if runner is None:
        wiring = AwsWiring(config)
        runner = wiring.run
        on_internal_failure = on_internal_failure or wiring.note_internal_failure
    app = FastAPI(title="ViharBot Kepler steward", version="0.1", docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=MAX_BODY_BYTES,
                       paths=frozenset({"/invocations"}), agent="kepler")
    app.state.config = config
    running = threading.Lock()  # one poll at a time per container; the database guards across them

    @app.get("/ping", include_in_schema=False)
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations", include_in_schema=False)
    def invoke(envelope: dict[str, Any], x_request_id: str | None = Header(default=None)):
        if "input" in envelope:
            if set(envelope) - _ENVELOPE_KEYS or not isinstance(envelope["input"], dict):
                raise HTTPException(422, "Invalid invocation envelope")
            payload, request_id = envelope["input"], envelope.get("request_id")
        else:
            payload, request_id = envelope, x_request_id
        if request_id is not None and (not isinstance(request_id, str) or len(request_id) > 128):
            raise HTTPException(422, "Invalid request id")
        try:
            invocation = Invocation.model_validate(payload)
        except ValidationError:
            raise HTTPException(422, "Invalid invocation payload") from None

        if not running.acquire(blocking=False):
            return JSONResponse({"status": "busy"}, status_code=409)
        try:
            result = runner(invocation, request_id)
        except Exception:  # never let a surprise leak a message, a token or a trace to the caller
            logger.error(json.dumps({"agent": "kepler", "event": "invocation_crashed",
                                     "decision": "fail", "error_code": "internal",
                                     "model_alias": None}, sort_keys=True))
            if on_internal_failure:
                on_internal_failure()
            return JSONResponse({"status": "failed", "error_code": "internal"}, status_code=500)
        finally:
            running.release()
        return JSONResponse(result.public(), status_code=502 if result.status == "failed" else 200)

    return app


def create_app_from_env(environ: Mapping[str, str] | None = None) -> FastAPI:
    """The uvicorn factory: `uvicorn --factory viharbot.steward.runtime:create_app_from_env`."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return create_app(from_env(dict(os.environ if environ is None else environ)))
