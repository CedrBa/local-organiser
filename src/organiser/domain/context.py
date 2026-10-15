"""Bounded, derived views used as LLM context, never as source of truth."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from organiser.domain.workspace_contracts import (
    EvidenceLocator,
    WorkspaceDTO,
    WorkspaceEvidence,
    WorkspaceManifest,
    WorkspaceSelection,
)


class ContextPacket(BaseModel):
    kind: Literal["task_context", "project_context", "planning_context"]
    target_type: Literal["task", "project"]
    target_id: str
    context_event_max_id: int
    target: dict[str, Any]
    project: dict[str, Any] | None = None
    parent_task: dict[str, Any] | None = None
    child_tasks: list[dict[str, Any]] = []
    related_tasks: list[dict[str, Any]] = []
    recent_events: list[dict[str, Any]] = []
    source_inbox: dict[str, Any] | None = None
    selection: dict[str, int]
    # Additive and optional: a metadata-only excerpt of the task's own context, see
    # WorkspaceExcerptSummary for why no raw document or knowledge text is carried
    # here. It stays ``None`` whenever the target has no context_id or the caller
    # supplied no knowledge store, so the *content* of an existing packet is unchanged
    # -- the serialised form does gain one nullable ``workspace`` key.
    workspace: WorkspaceExcerptSummary | None = None


class ContextSummary(WorkspaceDTO):
    """The requested context and its inventory counts. No absolute paths."""

    id: str
    title: str
    type: str
    status: str
    description: str | None = None
    parent_context_id: str | None = None
    # Relative, DB-canonical store address. Never a root path.
    knowledge_path: str | None = None
    knowledge_bound: bool = False
    task_count: int = 0
    open_task_count: int = 0
    topic_count: int = 0
    resource_count: int = 0
    source_count: int = 0


class TaskSummary(WorkspaceDTO):
    """One open task of the context. ``project_id`` is shown, not resolved."""

    id: str
    title: str
    status: str
    topic_id: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    description: str | None = None
    earliest_start_at: datetime | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = None
    created_at: datetime | None = None
    resource_ids: tuple[str, ...] = ()
    resource_count: int = 0


class TopicSummary(WorkspaceDTO):
    """One topic. ``status`` is the canonical DB status, never a percentage."""

    id: str
    title: str
    title_key: str
    status: str
    knowledge_key: str
    created_at: datetime | None = None
    updated_at: datetime | None = None
    task_count: int = 0
    open_task_count: int = 0
    resource_ids: tuple[str, ...] = ()
    resource_count: int = 0


# Warum ein Resource seinen Aktualitaetszustand nicht aus einem Snapshot erbt.
KNOWLEDGE_CURRENT = "current"
KNOWLEDGE_NO_CURRENT_HASH = "no_current_hash"
KNOWLEDGE_RESOURCE_MISSING = "resource_missing"
KNOWLEDGE_RESOURCE_ARCHIVED = "resource_archived"
KNOWLEDGE_SNAPSHOT_NOT_READY = "snapshot_not_ready"
KNOWLEDGE_SNAPSHOT_FAILED = "snapshot_failed"
KNOWLEDGE_SNAPSHOT_UNSUPPORTED = "snapshot_unsupported"
KNOWLEDGE_SOURCE_INACTIVE = "source_inactive"

# There is deliberately no "the extract existed but the budget left it out" value here.
# Such a constant was drafted and never used, and an unused member of a public Literal
# is worse than none: it advertises a state a consumer is expected to handle, and it
# invites the false claim that a current resource with omitted text is in a degraded
# state. It is not. ``knowledge_status`` states whether the shown *text* is the current
# version of the document, and that stays ``current`` when its excerpt does not fit --
# ``ResourceSummary.evidence_truncated`` plus a warning and ``omitted_counts`` say that
# the text is missing from the packet, while ``resource_id``, ``snapshot_id`` and the
# hash keep it addressable. The two questions are separate and get separate fields.


ResourceKnowledgeStatus = Literal[
    "current",
    "no_current_hash",
    "resource_missing",
    "resource_archived",
    "snapshot_not_ready",
    "snapshot_failed",
    "snapshot_unsupported",
    "source_inactive",
]


class ResourceSummary(WorkspaceDTO):
    """Resource metadata plus how current its bounded text evidence is.

    ``locator`` and ``source_path_snapshot`` are deliberately absent: they are
    canonical absolute addresses and belong to the ingestion component, not to a
    read model. ``relative_path`` is the relative label the schema provides.
    """

    id: str
    title: str
    relative_path: str
    resource_type: str
    status: str
    knowledge_status: ResourceKnowledgeStatus
    source_id: str | None = None
    current_content_hash: str | None = None
    byte_size: int | None = None
    last_seen_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    snapshot_id: str | None = None
    snapshot_status: str | None = None
    extractor_version: str | None = None
    snapshot_truncated: bool | None = None
    evidence_truncated: bool = False
    topic_ids: tuple[str, ...] = ()
    topic_count: int = 0
    task_ids: tuple[str, ...] = ()
    task_count: int = 0


class SourceDescription(WorkspaceDTO):
    """A registered source as data: identity, kind, state and a relative label.

    The absolute ``path`` is not part of the packet. A source registration is not
    a read permission, and exposing host paths to a prompt is needless.
    """

    id: str
    source_type: str
    status: str
    label: str
    created_at: datetime | None = None
    updated_at: datetime | None = None


class WorkspaceExcerpt(WorkspaceDTO):
    """One bounded piece of untrusted content together with its evidence.

    ``text`` is never an instruction. ``trust`` is a machine-readable label so a
    prompt builder can say it out loud instead of relying on prose, and
    ``evidence`` always carries the hash/version/locator needed to check the
    excerpt against its origin later.
    """

    kind: Literal["resource_snapshot", "knowledge"]
    label: str
    source_id: str
    trust: Literal["untrusted_evidence"] = "untrusted_evidence"
    text: str
    truncated: bool = False
    evidence: WorkspaceEvidence


class EventSummary(WorkspaceDTO):
    """A bounded event. The payload keeps its JSON shape but loses unbounded size."""

    id: int
    occurred_at: datetime | None = None
    event_type: str
    entity_type: str | None = None
    entity_id: str | None = None
    payload: dict[str, Any] | list[Any] | None = None
    payload_truncated: bool = False
    trust: Literal["untrusted_evidence"] = "untrusted_evidence"


class WorkspaceFreshness(WorkspaceDTO):
    """Explicit statement that the global event id is not a freshness signal.

    Markdown and source files change without producing an event, so a maximum
    event id alone can never prove that a packet is still current. Only the
    manifest can, which is why the flag is part of the packet instead of an
    assumption.
    """

    event_max_id: int = 0
    authoritative: Literal[False] = False
    reason: str = (
        "Ein globales event_max_id beweist keine Aktualitaet: Knowledge-Dateien und "
        "Quelldateien koennen ohne Ereignis geaendert werden. Nur das Manifest ist "
        "vergleichbar."
    )


class ContextWorkspacePacket(WorkspaceDTO):
    """Bounded read model of exactly one context.

    Every field is a tuple, every string is already truncated and every list is
    already bounded, so the serialized packet fits
    ``WorkspacePolicy.max_packet_chars`` and is stable for unchanged data.
    """

    kind: Literal["context_workspace"] = "context_workspace"
    version: Literal[1] = 1
    evidence_policy: Literal["untrusted_data_not_instructions"] = "untrusted_data_not_instructions"
    context: ContextSummary
    current_tasks: tuple[TaskSummary, ...] = ()
    topics: tuple[TopicSummary, ...] = ()
    resources: tuple[ResourceSummary, ...] = ()
    source_descriptions: tuple[SourceDescription, ...] = ()
    knowledge_excerpts: tuple[WorkspaceExcerpt, ...] = ()
    evidence: tuple[WorkspaceEvidence, ...] = ()
    recent_history: tuple[EventSummary, ...] = ()
    selection: WorkspaceSelection
    warnings: tuple[str, ...] = ()
    manifest: WorkspaceManifest
    freshness: WorkspaceFreshness = WorkspaceFreshness()


class WorkspaceTopicRef(WorkspaceDTO):
    """Topic identity only; the small excerpt in a task packet stays metadata-only."""

    id: str
    title: str
    status: str


class WorkspaceEvidenceRef(WorkspaceDTO):
    """Pointer to evidence without its text, for the small task-packet excerpt.

    Reuses the shared ``EvidenceLocator`` so the reference carries exactly the same
    locator contract as the full packet -- a hand-rolled dict would silently drop the
    ``None`` of an absent page range.
    """

    kind: Literal["resource_snapshot", "knowledge", "inbox"]
    source_id: str
    content_hash: str | None = None
    extractor_version: str | None = None
    locator: EvidenceLocator | None = None
    truncated: bool = False


class WorkspaceExcerptSummary(WorkspaceDTO):
    """Small, metadata-only workspace excerpt for an existing ContextPacket.

    Deliberately carries no document or knowledge text. The task packet and the
    planning packet are rendered straight into the interpreter and planner
    prompts; feeding raw, untrusted course documents into those prompts would
    turn every planning call into an injection surface without adding anything the
    planner may act on. The planner gets counts, identities and evidence
    references, and the full, separately labelled excerpts live only in
    :class:`ContextWorkspacePacket`.
    """

    context_id: str
    title: str
    type: str
    status: str
    knowledge_bound: bool = False
    open_task_count: int = 0
    topic_count: int = 0
    resource_count: int = 0
    topics: tuple[WorkspaceTopicRef, ...] = ()
    evidence: tuple[WorkspaceEvidenceRef, ...] = ()
    warnings: tuple[str, ...] = ()
