"""JSON contracts exchanged by the Phase 1 agents."""

from typing import Literal

from pydantic import BaseModel, Field


class Finding(BaseModel):
    claim: str = Field(description="One concise research finding")
    evidence: str = Field(description="Reasoning or source detail supporting the claim")
    confidence: Literal["low", "medium", "high"]


class ResearchFindings(BaseModel):
    summary: str
    findings: list[Finding]
    sources: list[str] = Field(description="Sources or references, if known")
    uncertainties: list[str]


class SecurityAssessment(BaseModel):
    overall_risk: Literal["low", "medium", "high"]
    implications: list[str]
    recommendations: list[str]
    uncertainties: list[str]
