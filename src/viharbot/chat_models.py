"""The public chat request and response models, shared by /api/chat and the live event stream.

They live here, not in web_app.py, so the event contract can reuse the real ChatResponse without
importing the web application. web_app re-exports them, so existing imports are unchanged.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .orchestration_view import OrchestrationMeta
from .schemas import (
    FactCheckReview,
    FinalSynthesis,
    PrivacyAssessment,
    ResearchFindings,
    SecurityAssessment,
    SourceQualityReview,
)


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=3000)
    history: list[ChatTurn] = Field(default_factory=list, max_length=4)


class AgentActivity(BaseModel):
    agent: str
    display_name: str
    model_alias: str | None = None
    duration_ms: float
    status: Literal["complete", "failed"]


class ChatResponse(BaseModel):
    request_id: str
    findings: ResearchFindings
    assessment: SecurityAssessment
    fact_check: FactCheckReview | None = None
    source_quality: SourceQualityReview | None = None
    privacy_review: PrivacyAssessment | None = None
    synthesis: FinalSynthesis | None = None
    agent_activity: list[AgentActivity]
    # Present only for iterative orchestration; omitted from every other response.
    orchestration: OrchestrationMeta | None = None
