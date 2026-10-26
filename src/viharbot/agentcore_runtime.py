"""Small HTTP adapter for the Amazon Bedrock AgentCore Runtime contract."""

from __future__ import annotations

from typing import Any, Callable, Type

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError


def add_agentcore_routes(
    app: FastAPI,
    request_model: Type[BaseModel],
    handler: Callable[[BaseModel, Response, str | None], Any],
    *,
    dependencies: list[Any] | None = None,
) -> None:
    """Expose the existing JSON handler through AgentCore's /ping and /invocations."""

    @app.get("/ping", include_in_schema=False)
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations", include_in_schema=False, dependencies=dependencies or [])
    def invoke(
        envelope: dict[str, Any],
        x_request_id: str | None = Header(default=None),
    ) -> JSONResponse:
        payload = envelope.get("input", envelope)
        request_id = envelope.get("request_id") or x_request_id
        try:
            request = request_model.model_validate(payload)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="Invalid invocation payload") from exc

        response = Response()
        result = handler(request, response, request_id)
        headers = dict(response.headers)
        if request_id:
            headers.setdefault("X-Request-ID", request_id)
        return JSONResponse(
            content=jsonable_encoder(result),
            status_code=response.status_code,
            headers=headers,
        )
