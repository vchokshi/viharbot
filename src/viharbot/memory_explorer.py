"""Agent Memory Explorer: a read-only, founder-only window into agent working state.

Phase 1 (this module) serves the page shell, the agent list from the canonical registry, and
Charlie's working-memory projection. Persistent memory and a founder event stream are reported as
`not_connected`; see docs/agent-memory-explorer.md for the contract and the prerequisites.

Authorization is server-side and deny-by-default. Until shared IAM and sessions land (#338, #343),
the interim gate is two separate bearer tokens from the environment: one grants metadata, the other
also grants content. This is a local and test convention, not the production design.
"""

from __future__ import annotations

import hmac
import logging
import os
import re
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .execution_bindings import default_bindings
from .explorer_links import command_central_url, environment_name, explorer_path
from .identity_registry import default_registry
from .team_profiles import PORTRAITS
from .working_memory import SCHEMA, explorer_enabled, registry as working_registry

logger = logging.getLogger("web")
router = APIRouter()

METADATA_TOKEN = "MEMORY_EXPLORER_FOUNDER_TOKEN"
CONTENT_TOKEN = "MEMORY_EXPLORER_CONTENT_TOKEN"
MIN_TOKEN_CHARS = 24
_AGENT_ID = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
Permission = Literal["metadata", "content"]


def _audit(event: str, **fields: object) -> None:
    # Fixed codes only: never a token, an agent payload or any memory content.
    logger.info(event, extra={"agent": "web", "action_status": event, **fields})


def _token(name: str) -> bytes | None:
    value = os.getenv(name, "").strip()
    return value.encode() if len(value) >= MIN_TOKEN_CHARS else None


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail={"state": "unavailable", "reason": "explorer_disabled"})


def require_founder(authorization: str | None = Header(default=None)) -> Permission:
    """Deny by default. Returns the highest permission the presented token grants."""
    if not explorer_enabled():
        raise _unavailable()
    metadata, content = _token(METADATA_TOKEN), _token(CONTENT_TOKEN)
    if metadata is None and content is None:
        _audit("explorer_denied", error="not_configured")
        raise HTTPException(status_code=503, detail={"state": "unavailable", "reason": "not_configured"})
    scheme, _, supplied = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        _audit("explorer_denied", error="missing_credentials")
        raise HTTPException(status_code=401, detail={"state": "unauthorized"},
                            headers={"WWW-Authenticate": "Bearer"})
    given = supplied.strip().encode()
    # Evaluate both comparisons so timing does not reveal which token matched.
    is_content = content is not None and hmac.compare_digest(given, content)
    is_metadata = metadata is not None and hmac.compare_digest(given, metadata)
    if is_content:
        return "content"
    if is_metadata:
        return "metadata"
    _audit("explorer_denied", error="invalid_credentials")
    raise HTTPException(status_code=401, detail={"state": "unauthorized"},
                        headers={"WWW-Authenticate": "Bearer"})


def _known_agent(agent_id: str) -> str:
    if not _AGENT_ID.fullmatch(agent_id) or agent_id not in default_registry():
        raise HTTPException(status_code=404, detail={"state": "unknown_agent"})
    return agent_id


def _capabilities(agent_id: str) -> tuple[list[str], list[str]]:
    identity = default_registry().get(agent_id)
    bound = sorted(b.capability.value for b in default_bindings().bindings
                   if b.identity_id == agent_id and b.enabled)
    return [c.value for c in identity.capabilities], bound


def agent_summary(agent_id: str) -> dict:
    identity = default_registry().get(agent_id)
    declared, bound = _capabilities(agent_id)
    portrait = PORTRAITS.get(agent_id)
    return {
        "id": identity.id, "name": identity.name, "title": identity.title,
        "description": identity.description,
        "portrait": f"/images/{portrait.file}" if portrait else None,
        "portrait_alt": portrait.alt if portrait else "",
        "path": explorer_path(identity.id),
        # An identity is not a running agent: only enabled execution bindings run anything.
        "runtime": "bound" if bound else "no_execution_binding",
        "capabilities": declared, "bound_capabilities": bound,
        "working_memory": "connected" if agent_id == "charlie" else "not_connected",
        "persistent_memory": "not_connected",
    }


def _sections() -> list[dict]:
    """The proposed memory categories, none of them connected in phase 1 beyond working memory."""
    def row(key: str, label: str, state: str, note: str) -> dict:
        return {"key": key, "label": label, "state": state, "note": note}
    return [
        row("working", "Working memory", "connected", "Charlie's per-request orchestration state."),
        row("persistent", "Persistent memory", "not_connected",
            "Governed memory service records (#40); founder read access arrives in phase 2."),
        row("episodic", "Episodic memory", "not_implemented", "No contract exists yet."),
        row("semantic", "Semantic memory", "not_implemented", "No contract exists yet."),
        row("procedural", "Procedural memory", "not_implemented", "No contract exists yet."),
        row("relationship", "Relationship memory", "not_implemented", "Planned under #300, #302 and #303."),
    ]


@router.get("/api/memory-explorer/status", include_in_schema=False)
def explorer_status() -> dict:
    """Public and content-free: only whether the explorer is switched on."""
    return {"state": "enabled" if explorer_enabled() else "unavailable"}


@router.get("/api/memory-explorer/config", include_in_schema=False)
def explorer_config(permission: Permission = Depends(require_founder)) -> dict:
    central = command_central_url()
    return {
        "environment": environment_name(), "permission": permission, "default_agent": "charlie",
        "authentication": "interim_bearer_token",
        "command_central": {"state": "available" if central else "planned", "url": central or None,
                            "label": "Command Central"},
    }


@router.get("/api/memory-explorer/agents", include_in_schema=False)
def explorer_agents(_: Permission = Depends(require_founder)) -> dict:
    return {"agents": [agent_summary(i.id) for i in default_registry().list()]}


@router.get("/api/memory-explorer/agents/{agent_id}", include_in_schema=False)
def explorer_agent(agent_id: str, _: Permission = Depends(require_founder)) -> dict:
    agent = agent_summary(_known_agent(agent_id))
    return {**agent, "sections": _sections() if agent_id == "charlie" else [
        {"key": "working", "label": "Working memory", "state": "not_connected",
         "note": "No working-state contract is connected for this agent."}]}


@router.get("/api/memory-explorer/agents/{agent_id}/working-memory", include_in_schema=False)
def explorer_working_memory(agent_id: str, permission: Permission = Depends(require_founder)) -> JSONResponse:
    agent_id = _known_agent(agent_id)
    if agent_id != "charlie":  # never answer with another agent's data
        return JSONResponse({"state": "not_connected", "agent": agent_id, "schema_version": SCHEMA,
                             "runs": []}, headers={"Cache-Control": "no-store"})
    content = permission == "content"
    view = working_registry.view(content=content)
    _audit("explorer_read_working_content" if content else "explorer_read_working_metadata")
    return JSONResponse(view.model_dump(mode="json"), headers={"Cache-Control": "no-store"})


@router.get("/api/memory-explorer/agents/{agent_id}/persistent-memory", include_in_schema=False)
def explorer_persistent_memory(agent_id: str, _: Permission = Depends(require_founder)) -> JSONResponse:
    agent_id = _known_agent(agent_id)
    return JSONResponse({
        "state": "not_connected", "agent": agent_id,
        "namespace": f"/memory/agent/{agent_id}/records" if agent_id == "charlie" else None,
        "reason": "no founder-scoped read contract exists on the memory service yet (phase 2)",
        "records": []}, headers={"Cache-Control": "no-store"})


@router.get("/api/memory-explorer/agents/{agent_id}/events", include_in_schema=False)
def explorer_events(agent_id: str, _: Permission = Depends(require_founder)) -> JSONResponse:
    agent_id = _known_agent(agent_id)
    return JSONResponse({
        "state": "not_connected", "agent": agent_id,
        "reason": "the existing SSE stream is request-scoped and public-safe; a founder stream "
                  "needs a versioned, authorized contract (#348)",
        "events": []}, headers={"Cache-Control": "no-store"})


def shell_html(images_base: str) -> HTMLResponse:
    path = Path(os.getenv("WEB_STATIC_DIR", Path(__file__).with_name("static"))) / "memory-explorer.html"
    page = path.read_text(encoding="utf-8")
    if images_base:
        page = page.replace("/images/", images_base + "/")
    return HTMLResponse(page, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
