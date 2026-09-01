"""成员 A 与成员 B 之间共享的标准工单数据契约。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field


class DialogueTurn(BaseModel):
    role: Literal["operator", "citizen", "unknown"]
    speaker: str
    text: str = Field(..., min_length=1, max_length=2000)


class FormattedTranscript(BaseModel):
    formatted_text: str
    citizen_text: str
    turns: list[DialogueTurn] = Field(default_factory=list)
    mode: Literal["acoustic", "heuristic", "labeled", "unsegmented"] = "unsegmented"


class AppealSource(BaseModel):
    type: Literal["text", "audio"] = "text"
    raw_text: str = Field(..., min_length=2, max_length=20000)
    masked_text: str = ""
    audio_file_name: str | None = None
    received_at: str | None = None
    source_channel: str = "12345热线"
    formatted_text: str = ""
    citizen_text: str = ""
    dialogue_turns: list[DialogueTurn] = Field(default_factory=list)
    role_format_mode: Literal["acoustic", "heuristic", "labeled", "unsegmented"] = "unsegmented"


class WorkOrderElements(BaseModel):
    time: str = ""
    time_basis: Literal["stated", "received_at"] = "stated"
    location: str = ""
    subjects: list[str] = Field(default_factory=list)
    event: str = ""
    request: str = ""
    contact_hint: str = ""


class WorkOrderQuality(BaseModel):
    completeness: float = Field(ge=0, le=1)
    fidelity: float = Field(ge=0, le=1)
    clarity: float = Field(ge=0, le=1)
    overall: float = Field(ge=0, le=1)
    warnings: list[str] = Field(default_factory=list)


class WorkOrderGeneration(BaseModel):
    mode: Literal["llm", "rule_fallback"] = "rule_fallback"
    provider: str = "rules"
    model: str = ""
    fallback_reason: str = ""
    evidence: dict[str, list[str]] = Field(default_factory=dict)


class MatterCandidate(BaseModel):
    id: str
    topic: str
    description: str
    evidence: str


class StandardWorkOrder(BaseModel):
    case_id: str
    title: str
    summary: str
    content: str
    region: str = ""
    source: AppealSource
    elements: WorkOrderElements
    missing_fields: list[str] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)
    clarification_questions: list[str] = Field(default_factory=list)
    quality: WorkOrderQuality
    generation: WorkOrderGeneration = Field(default_factory=WorkOrderGeneration)
    multiple_matters: bool = False
    matter_candidates: list[MatterCandidate] = Field(default_factory=list)
    parent_case_id: str | None = None
    matter_index: int | None = None
    status: Literal["draft", "confirmed"] = "draft"
    requires_human_review: bool = True
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    confirmed_at: str | None = None
    confirmed_by: str | None = None
    handoff_status: Literal["not_ready", "pending", "processing", "completed"] = "not_ready"
    handed_off_at: str | None = None
    claimed_at: str | None = None
    claimed_by: str | None = None
    completed_at: str | None = None
    classification: dict | None = None
    routing: dict | None = None
    reply: dict | None = None


class IntakeAnalyzeRequest(BaseModel):
    text: str = Field(..., min_length=2, max_length=20000)
    source_type: Literal["text", "audio"] = "text"
    audio_file_name: str | None = None
    raw_transcript: str | None = Field(default=None, max_length=30000)
    received_at: str | None = None
    source_channel: str = "12345热线"


class IntakeConfirmRequest(BaseModel):
    workorder: StandardWorkOrder


class IntakeHandoffResultRequest(BaseModel):
    classification: dict
    routing: dict
    reply: dict | None = None


class ClarificationAnswers(BaseModel):
    time: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=300)
    event: str | None = Field(default=None, max_length=1000)
    request: str | None = Field(default=None, max_length=600)
    additional_details: str | None = Field(default=None, max_length=1000)


class IntakeClarifyRequest(BaseModel):
    workorder: StandardWorkOrder
    answers: ClarificationAnswers


class IntakeSplitRequest(BaseModel):
    workorder: StandardWorkOrder
    candidate_ids: list[str] = Field(default_factory=list, min_length=2, max_length=10)
