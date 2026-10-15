"""Domain DTOs and contracts for context-bound action proposals and review."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import Field

from organiser.domain.workspace_contracts import (
    EvidenceLocator,
    TopicStatus,
    WorkspaceDTO,
)
from organiser.domain.knowledge_path import validate_context_path, validate_knowledge_path
from organiser.domain.topics import knowledge_key_for

MAX_PROPOSALS_PER_GENERATION = 5
MAX_PROPOSAL_OUTPUT_TOKENS = 1200
MAX_PROPOSAL_OUTPUT_CHARS = 12000
MAX_INBOX_EVIDENCE_CHARS = 4000


def knowledge_target_path(knowledge_path: str, target: str, topic_id: str | None = None) -> str:
    """Canonical store-relative destination; never supplied by the model."""
    root = validate_context_path(knowledge_path)
    if target == "topic" and topic_id:
        filename = f"topics/{knowledge_key_for(topic_id)}.md"
    else:
        filenames = {"overview": "overview.md", "progress": "progress.md", "note": "notes.md"}
        if target not in filenames:
            raise ValueError("Ungueltiges Knowledge-Target")
        filename = filenames[target]
    return validate_knowledge_path(f"{root}/{filename}", allow_reserved_segments=True)


class ActionBase(WorkspaceDTO):
    """Base class for strictly typed, non-extensible action payloads."""


class CreateTopicAction(ActionBase):
    action: Literal["create_topic"] = "create_topic"
    title: str = Field(min_length=1, max_length=200)


class SetTopicStatusAction(ActionBase):
    action: Literal["set_topic_status"] = "set_topic_status"
    topic_id: str = Field(min_length=1, max_length=64)
    status: TopicStatus


class LinkResourceTopicAction(ActionBase):
    action: Literal["link_resource_topic"] = "link_resource_topic"
    topic_id: str = Field(min_length=1, max_length=64)
    resource_id: str = Field(min_length=1, max_length=64)


class CreateTaskAction(ActionBase):
    action: Literal["create_task"] = "create_task"
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    topic_id: str | None = Field(default=None, max_length=64)
    resource_ids: tuple[str, ...] = Field(default=(), max_length=10)
    due_at: datetime | None = None
    earliest_start_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    cognitive_load: Literal["high", "medium", "low", "physical"] = "medium"


class LinkTaskContextAction(ActionBase):
    action: Literal["link_task_context"] = "link_task_context"
    task_id: str = Field(min_length=1, max_length=64)
    topic_id: str | None = Field(default=None, max_length=64)


class UpdateKnowledgeAction(ActionBase):
    action: Literal["update_knowledge"] = "update_knowledge"
    target: Literal["overview", "progress", "note", "topic"]
    topic_id: str | None = Field(default=None, max_length=64)
    text: str | None = Field(default=None, max_length=20_000)
    heading: str | None = Field(default=None, max_length=200)
    percent: int | None = Field(default=None, ge=0, le=100)


ActionPayload = Annotated[
    Union[
        CreateTopicAction,
        SetTopicStatusAction,
        LinkResourceTopicAction,
        CreateTaskAction,
        LinkTaskContextAction,
        UpdateKnowledgeAction,
    ],
    Field(discriminator="action"),
]


class ProposalEvidence(WorkspaceDTO):
    kind: Literal["resource_snapshot", "inbox"]
    source_id: str = Field(min_length=1, max_length=64)
    content_hash: str | None = None
    locator: EvidenceLocator | None = None
    truncated: bool = False


class ActionProposalEnvelope(WorkspaceDTO):
    action: ActionPayload
    evidence: ProposalEvidence
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    uncertainty: str | None = Field(default=None, max_length=1000)
    missing_fields: tuple[str, ...] = Field(default=(), max_length=10)
    needs_review: bool = False


class GenerationOutput(WorkspaceDTO):
    proposals: tuple[ActionProposalEnvelope, ...] = Field(
        default=(), max_length=MAX_PROPOSALS_PER_GENERATION
    )


class ProposalValidationError(ValueError):
    """Raised when model output fails structural or semantic validation."""

    def __init__(self, message: str, code: str = "invalid_proposal") -> None:
        super().__init__(message)
        self.code = code
