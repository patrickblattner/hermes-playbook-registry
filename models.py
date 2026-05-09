"""Pydantic models für Request/Response der Registry API."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


# ---------- Submission ----------

class CandidateSubmission(BaseModel):
    skill_id: str = Field(..., min_length=1, max_length=200)
    problem_domain: str = Field(..., min_length=1, max_length=200)
    problem_description: str = Field(..., min_length=1)
    approach: str = Field(..., min_length=1)
    content: str = Field(..., min_length=1)
    author_agent: str = Field(..., min_length=1, max_length=100)
    metadata: dict[str, Any] | None = None


class CandidateResponse(BaseModel):
    id: int
    skill_id: str
    version: int
    status: str


# ---------- Validation ----------

class ValidationSubmission(BaseModel):
    validator_agent: str = Field(..., min_length=1, max_length=100)
    success: bool
    latency_ms: int | None = None
    model_used: str | None = None
    notes: str | None = None


class ValidationResponse(BaseModel):
    id: int
    playbook_id: int
    recorded: bool


# ---------- Playbook (vollständig) ----------

class PlaybookOut(BaseModel):
    id: int
    skill_id: str
    version: int
    status: str
    problem_domain: str
    problem_description: str
    approach: str
    content: str
    author_agent: str
    created_at: datetime
    promoted_at: datetime | None = None
    metadata: dict[str, Any] | None = None
    # aggregierte Felder aus playbook_stats view
    validation_count: int = 0
    success_rate: float = 0.0
    avg_latency_ms: float | None = None


class PlaybookWithValidations(PlaybookOut):
    validations: list["ValidationOut"] = []


class ValidationOut(BaseModel):
    id: int
    playbook_id: int
    validator_agent: str
    success: bool
    latency_ms: int | None = None
    model_used: str | None = None
    notes: str | None = None
    validated_at: datetime


# ---------- Search ----------

class SearchResponse(BaseModel):
    query: str
    total: int
    results: list[PlaybookOut]


# ---------- Promotion ----------

class PromoteResponse(BaseModel):
    id: int
    skill_id: str
    version: int
    status: Literal["verified"]
    promoted_at: datetime


# ---------- Health ----------

class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    db: Literal["connected", "disconnected"]
    journal_mode: str | None = None
