"""Validated interpreter proposals; they do not perform domain changes."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


ProposalAction = Literal[
    "create_task",
    "update_task",
    "start_task",
    "complete_task",
    "cancel_task",
    "reschedule_task",
    "create_project",
]


class InterpretationProposal(BaseModel):
    source_inbox_id: str = Field(min_length=1)
    action: ProposalAction
    target_entity_id: str | None = None
    title: str | None = None
    description: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    earliest_start_at: datetime | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class ManualTriageRequest(BaseModel):
    proposal: InterpretationProposal


class TriageRunResult(BaseModel):
    run_id: str
    status: str
    source_inbox_id: str
    entity_type: str | None = None
    entity_id: str | None = None


PlanningActionType = Literal[
    "create_task",
    "update_task",
    "start_task",
    "complete_task",
    "cancel_task",
    "reschedule_task",
]


class PlanningAction(BaseModel):
    action: PlanningActionType
    target_task_id: str | None = None
    title: str | None = None
    description: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    earliest_start_at: datetime | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = None
    rationale: str = ""
    confidence: float | None = Field(default=None, ge=0, le=1)
    uncertainty: str | float | None = None
    missing_fields: list[str] = Field(default_factory=list)


class PlanningProposal(BaseModel):
    target_type: Literal["task", "project"]
    target_id: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    actions: list[PlanningAction] = Field(min_length=1, max_length=3)


class PlanningProposalResult(BaseModel):
    id: str
    status: Literal["proposed", "applied", "failed"]
    target_type: Literal["task", "project"]
    target_id: str
    model: str | None
    context_event_max_id: int
    context: dict
    raw_output: str | None
    proposal: PlanningProposal | None
    error: str | None
