"""FastAPI surface for founder-authorized Kepler assignments (issue #128).

Authentication reuses service_auth: every route except /health needs a verified, request-bound
assertion carrying the route's capability, and the caller's registry subject must also be named in
Kepler's policy as an assigner. There is no switch to turn authentication off; `create_app`
requires an authenticator. The surface is deliberately tiny: authorize a run for an issue, read it,
cancel it. There is no route to merge, push, deploy or change policy; the work itself is driven
in-process by the agent loop (#127).

Intended to be bound to a loopback or private interface and called with a signed assertion (mint
one with `service_auth.mint_assertion`, audience "kepler"). Not wired into compose, Dockerfiles or
deploy files, and there is no module-level app. Local interfaces are otherwise unauthenticated
beyond the signature, and the replay cache is per process (see docs/service-authentication.md).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from ..observability import get_request_id
from ..service_auth import Caller, ServiceAuthenticator, require_assertion
from .policy import AssignmentDenied
from .service import AssignmentService
from .store import StoreError

ASSIGN_CAPABILITY = "kepler.assign"
READ_CAPABILITY = "kepler.read"
AUDIENCE = "kepler"


class AssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue: StrictInt = Field(gt=0, lt=10**9)


def create_app(service: AssignmentService, authenticator: ServiceAuthenticator) -> FastAPI:
    # No interactive docs or schema routes: a control surface exposes only what it needs.
    app = FastAPI(
        title="ViharBot Kepler", version="0.1", docs_url=None, redoc_url=None, openapi_url=None
    )
    app.state.service = service

    def known(caller: Caller) -> str:
        if caller.sub not in service.policy.assigners:
            raise HTTPException(403, "authentication failed")
        return caller.sub

    def assigner(
        caller: Caller = Depends(require_assertion(authenticator, ASSIGN_CAPABILITY)),
    ) -> str:
        return known(caller)

    def reader(caller: Caller = Depends(require_assertion(authenticator, READ_CAPABILITY))) -> str:
        return known(caller)

    def rid(request: Request, response: Response) -> str:
        value = get_request_id(request.headers.get("x-request-id")) or str(uuid4())
        response.headers["X-Request-ID"] = value
        return value

    def run(call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except AssignmentDenied as denied:
            raise HTTPException(denied.status, denied.reason) from None
        except StoreError:
            raise HTTPException(503, "store unavailable") from None

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "agent": "kepler"}

    @app.post("/v1/assignments")
    def assign(
        payload: AssignRequest, actor: str = Depends(assigner), request_id: str = Depends(rid)
    ) -> dict:
        return run(service.authorize, payload.issue, actor=actor, request_id=request_id)

    @app.get("/v1/assignments/{run_id}")
    def read(run_id: str, actor: str = Depends(reader)) -> dict:
        return run(service.get, run_id)

    @app.post("/v1/assignments/{run_id}:cancel")
    def cancel(run_id: str, actor: str = Depends(assigner), request_id: str = Depends(rid)) -> dict:
        return run(service.cancel, run_id, actor=actor, request_id=request_id)

    return app
