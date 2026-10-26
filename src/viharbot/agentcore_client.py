"""JSON invocation client for AgentCore Runtime URLs used by the web coordinator."""

from __future__ import annotations

import json
import os
from typing import Any

_runtime_client: Any | None = None


def invoke_agentcore(runtime_arn: str, payload: dict, request_id: str) -> dict:
    """Invoke one runtime over AgentCore's authenticated HTTPS data plane."""
    global _runtime_client
    if _runtime_client is None:
        import boto3

        _runtime_client = boto3.client(
            "bedrock-agentcore",
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        )

    response = _runtime_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        runtimeSessionId=request_id,
        qualifier="DEFAULT",
        contentType="application/json",
        accept="application/json",
        payload=json.dumps({"request_id": request_id, "input": payload}).encode("utf-8"),
    )
    status_code = response.get("statusCode", 200)
    body = response["response"].read().decode("utf-8")
    if status_code >= 400:
        raise RuntimeError(f"AgentCore runtime returned HTTP {status_code}")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise ValueError("AgentCore runtime returned a non-object JSON response")
    return result
