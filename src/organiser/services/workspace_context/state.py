"""Internal prepared metadata and evidence candidates shared by workspace stages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from organiser.db.models import (
    Context,
    ContextSource,
    Event,
    Resource,
    ResourceSnapshot,
    Task,
    Topic,
)
from organiser.domain.context import (
    WorkspaceExcerpt,
)
from .policy import WorkspacePolicy
from .redaction import _bound_payload, _contains_absolute_path


# ---------------------------------------------------------------------------
# Prepared data (every DB read happens before the budget loop)
# ---------------------------------------------------------------------------


@dataclass
class _ExcerptCandidate:
    """One content excerpt, already identified, hashed and length-bounded.

    ``priority`` is a global order across both kinds: resource candidates first in
    their ranking order, then knowledge candidates in theirs. Excerpts are dropped
    from the highest priority number down, so the least relevant text is what
    disappears when the budget shrinks.

    ``text`` starts empty for snapshot candidates and is filled in later, and only
    for the few that survive the excerpt budget. That is why this one dataclass is
    mutable while every value it produces is frozen.
    """

    priority: int
    kind: str
    label: str
    source_id: str
    truncated: bool
    content_hash: str | None
    extractor_version: str | None
    text: str = ""
    text_loaded: bool = False
    snapshot: ResourceSnapshot | None = None
    resource_id: str | None = None


@dataclass
class _EventRow:
    """One selected event, read as a bounded row instead of as a full ORM entity.

    ``payload`` is ``None`` **and** ``payload_loaded`` is ``False`` when the stored
    JSON was larger than ``max_event_payload_bytes``: the size was measured in SQL and
    the blob was never handed to Python. ``payload_length`` is the measurement, so the
    manifest can record what was actually used -- a length -- instead of pretending it
    hashed something it never read.

    The bounded projection is computed once and reused. ``_assemble`` may call
    ``_assemble_once`` dozens of times; re-projecting every payload on every pass was
    the second half of the cost this class removes.
    """

    id: int
    occurred_at: datetime | None
    event_type: str | None
    entity_type: str | None
    entity_id: str | None
    payload: Any | None
    payload_length: int
    payload_loaded: bool
    payload_limit: int
    projected: dict[str, Any] | list[Any] | None = None
    projected_truncated: bool = False
    projected_ready: bool = False

    def projection(self, policy: WorkspacePolicy) -> tuple[dict[str, Any] | list[Any] | None, bool]:
        """The bounded payload, projected once."""
        if not self.projected_ready:
            payload, dropped = _bound_payload(self.payload, 0, policy)
            if payload is not None and not isinstance(payload, (dict, list)):
                payload = {"value": payload}
            if not self.payload_loaded:
                payload = {
                    "_omitted": (
                        f"payload mit {self.payload_length} Zeichen nicht geladen "
                        f"(Lesegrenze {self.payload_limit} Zeichen)"
                    )
                }
                dropped = True
            elif payload is not None:
                serialized = json.dumps(payload, default=str, ensure_ascii=False)
                if _contains_absolute_path(serialized):
                    payload = {"_omitted": "payload mit absolutem Pfad ausgelassen"}
                    dropped = True
            self.projected = payload
            self.projected_truncated = dropped
            self.projected_ready = True
        return self.projected, self.projected_truncated


@dataclass(frozen=True)
class _ExcerptSelection:
    """What one reduction pass chose, and why the rest was left out."""

    excerpts: tuple[WorkspaceExcerpt, ...]
    withheld_resource_ids: frozenset[str]
    omitted: int
    # Knowledge candidates that were opened successfully, held text, and still lost
    # their slot. This is the number that separates "readable but out of budget" from
    # "never even opened", and it is only knowable for the files that were opened --
    # claiming the rest would be guessing.
    readable_omitted: int


@dataclass
class _Prepared:
    """Everything the assembler needs, already bounded by SQL LIMITs or COUNTs."""

    context: Context
    event_max_id: int
    counts: dict[str, int]
    tasks: list[Task] = field(default_factory=list)
    topics: list[Topic] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    sources: list[ContextSource] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    source_by_id: dict[str, ContextSource] = field(default_factory=dict)
    # The sources actually *referenced* by the selected resources. Kept apart from
    # ``source_by_id`` (the first ``max_sources`` for display) on purpose: whether a
    # resource's source is still active decides whether its text may be shown, and
    # that check must never depend on how many sources fit into the display budget.
    referenced_sources: dict[str, ContextSource] = field(default_factory=dict)
    unusable_sources: dict[str, ContextSource] = field(default_factory=dict)
    latest_snapshot: dict[str, ResourceSnapshot] = field(default_factory=dict)
    current_snapshot: dict[str, ResourceSnapshot] = field(default_factory=dict)
    snapshot_by_id: dict[str, ResourceSnapshot] = field(default_factory=dict)
    # Character length of every current ready snapshot, measured in SQL so the blob
    # itself never has to be loaded just to find out whether it is empty.
    snapshot_text_lengths: dict[str, int] = field(default_factory=dict)
    resource_status: dict[str, str] = field(default_factory=dict)
    task_resource_links: dict[str, list[str]] = field(default_factory=dict)
    task_resource_counts: dict[str, int] = field(default_factory=dict)
    topic_resource_links: dict[str, list[str]] = field(default_factory=dict)
    topic_resource_counts: dict[str, int] = field(default_factory=dict)
    resource_topic_links: dict[str, list[str]] = field(default_factory=dict)
    resource_topic_counts: dict[str, int] = field(default_factory=dict)
    resource_task_links: dict[str, list[str]] = field(default_factory=dict)
    resource_task_counts: dict[str, int] = field(default_factory=dict)
    candidates: list[_ExcerptCandidate] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # How many resource excerpt candidates were identified in total, before any read
    # and before the excerpt budget cut them down.
    resource_candidate_total: int = 0
    # How many knowledge candidates were identified, before the excerpt budget cut
    # them down. Reported so a caller can tell "no markdown exists" from "the markdown
    # did not fit".
    knowledge_candidate_total: int = 0
    # Knowledge files that were *opened* and refused by the reader, and those that
    # opened fine but hold no text at all. Both are counted once per candidate, never
    # per budget pass: the difference between "nothing is readable" and "readable, but
    # it did not fit" is exactly this distinction, and a cumulative read counter
    # cannot express it.
    knowledge_reads_failed: int = 0
    knowledge_reads_empty: int = 0
