"""Shared workspace vocabulary and manifest DTOs; no services or ORM registry."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceType(StrEnum):
    PDF = "pdf"
    MARKDOWN = "markdown"
    TEXT = "text"
    OTHER = "other"


class ResourceStatus(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    ARCHIVED = "archived"


class SnapshotStatus(StrEnum):
    READY = "ready"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class TopicStatus(StrEnum):
    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"


class ProposalStatus(StrEnum):
    PROPOSED = "proposed"
    APPLYING = "applying"
    APPLIED = "applied"
    REJECTED = "rejected"
    FAILED = "failed"
    AMBIGUOUS = "ambiguous"


class WorkspaceDTO(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceLocator(WorkspaceDTO):
    """Half-open character offsets into the snapshot/file; PDF pages are 1-based."""

    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)


class WorkspaceEvidence(WorkspaceDTO):
    kind: Literal["resource_snapshot", "knowledge", "inbox"]
    source_id: str
    content_hash: str | None = None
    extractor_version: str | None = None
    locator: EvidenceLocator | None = None
    truncated: bool = False


class EntityManifestEntry(WorkspaceDTO):
    entity_type: Literal["context", "task", "topic", "resource", "context_source", "event", "inbox"]
    entity_id: str
    status: str | None = None
    updated_at: datetime | None = None
    # Exact selected fields, including entities lacking updated_at. No raw source paths.
    selected_fields: dict = Field(default_factory=dict)


class SnapshotManifestEntry(WorkspaceDTO):
    snapshot_id: str
    resource_id: str
    content_hash: str
    extractor_version: str
    status: SnapshotStatus


class KnowledgeManifestEntry(WorkspaceDTO):
    context_id: str
    # Store-policy-relative address, never an operator-controlled absolute path.
    relative_path: str
    content_hash: str


class WorkspaceManifest(WorkspaceDTO):
    version: Literal[1] = 1
    entities: tuple[EntityManifestEntry, ...] = ()
    snapshots: tuple[SnapshotManifestEntry, ...] = ()
    knowledge: tuple[KnowledgeManifestEntry, ...] = ()


class WorkspaceSelection(WorkspaceDTO):
    context_id: str
    topic_ids: tuple[str, ...] = ()
    resource_ids: tuple[str, ...] = ()
    omitted_counts: dict[str, int] = Field(default_factory=dict)
