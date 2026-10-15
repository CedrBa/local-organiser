"""Project prepared state into bounded packet summaries and evidence references."""

from __future__ import annotations

from typing import Sequence
from organiser.db.models import (
    ContextSource,
    Resource,
    Task,
    Topic,
)
from organiser.domain.context import (
    KNOWLEDGE_NO_CURRENT_HASH,
    EventSummary,
    ResourceSummary,
    SourceDescription,
    TaskSummary,
    TopicSummary,
    WorkspaceExcerpt,
)
from organiser.domain.workspace_contracts import (
    EvidenceLocator,
    WorkspaceEvidence,
)
from .constants import OPEN_TASK_STATUSES
from .policy import WorkspacePolicy
from .redaction import _page_span, _source_label, _truncate
from .state import _EventRow, _ExcerptCandidate, _Prepared
from .values import _stamp


def _to_excerpt(candidate: _ExcerptCandidate, policy: WorkspacePolicy) -> WorkspaceExcerpt:
    """Attach the verifiable locator to an excerpt, using the final text length."""
    text = candidate.text
    page_start, page_end = (None, None)
    if candidate.kind == "resource_snapshot" and candidate.snapshot is not None:
        page_start, page_end = _page_span(candidate.snapshot.metadata_payload, 0, len(text))
    return WorkspaceExcerpt(
        kind=candidate.kind,
        label=candidate.label,
        source_id=candidate.source_id,
        text=text,
        truncated=candidate.truncated,
        evidence=WorkspaceEvidence(
            kind=candidate.kind,
            source_id=candidate.source_id,
            content_hash=candidate.content_hash,
            extractor_version=candidate.extractor_version,
            locator=EvidenceLocator(
                char_start=0, char_end=len(text), page_start=page_start, page_end=page_end
            ),
            truncated=candidate.truncated,
        ),
    )


def _packet_evidence(
    resources: Sequence[ResourceSummary],
    excerpts: Sequence[WorkspaceExcerpt],
    policy: WorkspacePolicy,
) -> tuple[WorkspaceEvidence, ...]:
    """Every evidence pointer the packet hands out, bounded and deduplicated."""
    entries: list[WorkspaceEvidence] = []
    seen: set[tuple[str, str]] = set()
    for excerpt in excerpts:
        key = (excerpt.evidence.kind, excerpt.evidence.source_id)
        if key in seen:
            continue
        seen.add(key)
        entries.append(excerpt.evidence)
    for resource in resources:
        if resource.snapshot_id is None:
            continue
        key = ("resource_snapshot", resource.snapshot_id)
        if key in seen:
            continue
        seen.add(key)
        entries.append(
            WorkspaceEvidence(
                kind="resource_snapshot",
                source_id=resource.snapshot_id,
                content_hash=resource.current_content_hash,
                extractor_version=resource.extractor_version,
                locator=None,
                truncated=bool(resource.snapshot_truncated),
            )
        )
    return tuple(entries[: policy.max_excerpts + policy.max_resources])


def _task_summary(task: Task, prepared: _Prepared, policy: WorkspacePolicy) -> TaskSummary:
    links = prepared.task_resource_links.get(task.id, [])
    return TaskSummary(
        id=task.id,
        title=_truncate(task.title, policy.max_title_chars)[0] or "",
        status=task.status,
        topic_id=task.topic_id,
        project_id=task.project_id,
        parent_task_id=task.parent_task_id,
        description=_truncate(task.description, policy.max_description_chars)[0],
        earliest_start_at=_stamp(task.earliest_start_at),
        due_at=_stamp(task.due_at),
        estimated_minutes=task.estimated_minutes,
        created_at=_stamp(task.created_at),
        resource_ids=tuple(links),
        resource_count=prepared.task_resource_counts.get(task.id, len(links)),
    )


def _topic_summary(topic: Topic, prepared: _Prepared, policy: WorkspacePolicy) -> TopicSummary:
    links = prepared.topic_resource_links.get(topic.id, [])
    return TopicSummary(
        id=topic.id,
        title=_truncate(topic.title, policy.max_title_chars)[0] or "",
        title_key=_truncate(topic.title_key, policy.max_title_chars)[0] or "",
        status=topic.status,
        knowledge_key=topic.knowledge_key,
        created_at=_stamp(topic.created_at),
        updated_at=_stamp(topic.updated_at),
        task_count=sum(1 for task in prepared.tasks if task.topic_id == topic.id),
        open_task_count=sum(
            1
            for task in prepared.tasks
            if task.topic_id == topic.id and task.status in OPEN_TASK_STATUSES
        ),
        resource_ids=tuple(links),
        resource_count=prepared.topic_resource_counts.get(topic.id, len(links)),
    )


def _resource_summary(
    resource: Resource, prepared: _Prepared, policy: WorkspacePolicy, evidence_withheld: bool
) -> ResourceSummary:
    snapshot = prepared.latest_snapshot.get(resource.id)
    return ResourceSummary(
        id=resource.id,
        title=_truncate(resource.title, policy.max_title_chars)[0] or "",
        relative_path=_truncate(resource.relative_path, policy.max_label_chars)[0] or "",
        resource_type=resource.resource_type,
        status=resource.status,
        knowledge_status=prepared.resource_status.get(resource.id, KNOWLEDGE_NO_CURRENT_HASH),
        source_id=resource.source_id,
        current_content_hash=resource.current_content_hash,
        byte_size=resource.byte_size,
        last_seen_at=_stamp(resource.last_seen_at),
        created_at=_stamp(resource.created_at),
        updated_at=_stamp(resource.updated_at),
        snapshot_id=snapshot.id if snapshot is not None else None,
        snapshot_status=snapshot.status if snapshot is not None else None,
        extractor_version=snapshot.extractor_version if snapshot is not None else None,
        snapshot_truncated=bool(snapshot.truncated) if snapshot is not None else None,
        evidence_truncated=evidence_withheld,
        topic_ids=tuple(prepared.resource_topic_links.get(resource.id, [])),
        topic_count=prepared.resource_topic_counts.get(resource.id, 0),
        task_ids=tuple(prepared.resource_task_links.get(resource.id, [])),
        task_count=prepared.resource_task_counts.get(resource.id, 0),
    )


def _source_summary(source: ContextSource, policy: WorkspacePolicy) -> SourceDescription:
    return SourceDescription(
        id=source.id,
        source_type=_truncate(source.source_type, policy.max_label_chars)[0] or "",
        status=_truncate(source.status, policy.max_label_chars)[0] or "",
        label=_truncate(_source_label(source), policy.max_label_chars)[0] or "",
        created_at=_stamp(source.created_at),
        updated_at=_stamp(source.updated_at),
    )


def _event_summary(event: _EventRow, policy: WorkspacePolicy) -> EventSummary:
    payload, dropped = event.projection(policy)
    return EventSummary(
        id=event.id,
        occurred_at=_stamp(event.occurred_at),
        event_type=_truncate(event.event_type, policy.max_event_type_chars)[0] or "",
        entity_type=_truncate(event.entity_type, policy.max_event_entity_chars)[0],
        entity_id=_truncate(event.entity_id, policy.max_event_entity_chars)[0],
        payload=payload,
        payload_truncated=dropped,
    )
