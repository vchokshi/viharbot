"""Minimal JSON REST client used by ResearchAgent to call SecurityAgent."""

from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .litellm_client import urlopen as no_redirect_urlopen
from .outbound_auth import OutboundAuthError, outbound_headers
from .schemas import ResearchFindings, SecurityAssessment


class SecurityClientError(RuntimeError):
    def __init__(self, error_type: str, latency_ms: float):
        super().__init__(error_type)
        self.error_type = error_type
        self.latency_ms = latency_ms


class SecurityResponseModel:
    def __init__(self, request_id: str, assessment: SecurityAssessment):
        self.request_id = request_id
        self.assessment = assessment


def send_findings_to_security(
    *, findings: ResearchFindings, request_id: str
) -> SecurityResponseModel:
    """Send findings to SecurityAgent and validate its JSON assessment."""
    runtime_arn = os.getenv("SECURITY_AGENTCORE_ARN", "").strip()
    if runtime_arn:
        started = time.perf_counter()
        try:
            from .agentcore_client import invoke_agentcore

            result = invoke_agentcore(
                runtime_arn,
                {"findings": findings.model_dump()},
                request_id,
            )
            returned_request_id = result["request_id"]
            assessment = SecurityAssessment.model_validate(result["assessment"])
            if returned_request_id != request_id:
                raise ValueError("SecurityAgent returned a different request ID")
        except Exception as exc:
            raise SecurityClientError(
                type(exc).__name__,
                round((time.perf_counter() - started) * 1000, 2),
            ) from None
        return SecurityResponseModel(request_id=returned_request_id, assessment=assessment)

    url = os.getenv("SECURITY_AGENT_URL", "http://security:8000/assess")
    data = json.dumps({"findings": findings.model_dump()}).encode("utf-8")
    headers = {"Content-Type": "application/json", "X-Request-ID": request_id}
    started = time.perf_counter()
    try:
        # Off by default (adds nothing). When on, Research signs with its own key and sub ("research"),
        # never the web key. A signing failure stops the call: there is no unsigned fallback.
        assertion = outbound_headers(
            url, destination="security", method="POST", body=data, request_id=request_id,
            default_subject="research", agent="research",
        )
    except OutboundAuthError:
        raise SecurityClientError(
            "OutboundAuthError", round((time.perf_counter() - started) * 1000, 2)
        ) from None
    headers.update(assertion)
    # A signed request never follows redirects, so the token cannot be forwarded to another origin.
    opener = no_redirect_urlopen if assertion else urlopen
    request = Request(url, data=data, headers=headers)
    try:
        with opener(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
        returned_request_id = result["request_id"]
        assessment = SecurityAssessment.model_validate(result["assessment"])
        if returned_request_id != request_id:
            raise ValueError("SecurityAgent returned a different request ID")
    except HTTPError as exc:
        error_type = f"HTTP_{exc.code}"
        raise SecurityClientError(
            error_type,
            round((time.perf_counter() - started) * 1000, 2),
        ) from None
    except (URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise SecurityClientError(
            type(exc).__name__,
            round((time.perf_counter() - started) * 1000, 2),
        ) from None

    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    return SecurityResponseModel(request_id=returned_request_id, assessment=assessment)
