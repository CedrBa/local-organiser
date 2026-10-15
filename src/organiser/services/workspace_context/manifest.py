"""Record precisely the entities, snapshots and knowledge used by a packet."""

from __future__ import annotations

from typing import Sequence
from organiser.db.models import (
    Context,
    ContextSource,
)
from organiser.domain.context import (
    EventSummary,
    ResourceSummary,
    SourceDescription,
    TaskSummary,
    TopicSummary,
    WorkspaceExcerpt,
)
from organiser.domain.workspace_contracts import (
    EntityManifestEntry,
    KnowledgeManifestEntry,
    SnapshotManifestEntry,
    WorkspaceManifest,
)
from .policy import WorkspacePolicy
from .redaction import _safe_knowledge_path, _source_label, _truncate
from .state import _Prepared
from .values import _digest_json, _stamp, _stamp_key


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------


def _build_manifest(
    context: Context,
    prepared: _Prepared,
    policy: WorkspacePolicy,
    tasks: Sequence[TaskSummary],
    topics: Sequence[TopicSummary],
    resources: Sequence[ResourceSummary],
    sources: Sequence[SourceDescription],
    events: Sequence[EventSummary],
    excerpts: Sequence[WorkspaceExcerpt],
) -> WorkspaceManifest:
    """Record exactly what the packet used, so a later apply can re-check it.

    A global event id is not part of this: knowledge files and source documents
    change without an event, so only per-entity state plus real file hashes can
    prove that a packet is still current. Absolute source paths are never recorded;
    knowledge entries use the store-policy-relative address.

    Every recorded text is the *bounded* value the packet actually shows, not the raw
    column. Recording a raw 200.000-character description would make the manifest
    itself unbounded -- and it would be the wrong thing to compare, because a change
    beyond the truncation point does not change what the packet said. Such a change is
    still caught by ``updated_at``, which is recorded next to it.
    """

    def title(value: str | None) -> str | None:
        return _truncate(value, policy.max_title_chars)[0]

    def description(value: str | None) -> str | None:
        # Always the standard bound, even when the packet had to drop the field
        # entirely under budget pressure. The manifest records what the *entity*
        # holds; the emergency reduction is a property of this one packet.
        return _truncate(value, policy.max_description_chars)[0]

    entities: list[EntityManifestEntry] = [
        EntityManifestEntry(
            entity_type="context",
            entity_id=context.id,
            status=context.status,
            updated_at=_stamp(context.updated_at),
            selected_fields={
                "title": title(context.title),
                "type": title(context.type),
                "status": context.status,
                "description": description(context.description),
                "parent_context_id": context.parent_context_id,
                "knowledge_path": _safe_knowledge_path(context.knowledge_path),
            },
        )
    ]
    task_by_id = {task.id: task for task in prepared.tasks}
    for item in tasks:
        row = task_by_id.get(item.id)
        entities.append(
            EntityManifestEntry(
                entity_type="task",
                entity_id=item.id,
                status=item.status,
                updated_at=_stamp(row.updated_at) if row is not None else None,
                selected_fields={
                    "title": title(row.title if row is not None else item.title),
                    "status": item.status,
                    "topic_id": item.topic_id,
                    "parent_task_id": item.parent_task_id,
                    "project_id": item.project_id,
                    "description": description(
                        row.description if row is not None else item.description
                    ),
                    "due_at": _stamp_key(row.due_at) if row is not None else "",
                    "earliest_start_at": _stamp_key(row.earliest_start_at)
                    if row is not None
                    else "",
                    "estimated_minutes": item.estimated_minutes,
                    "created_at": _stamp_key(row.created_at) if row is not None else "",
                    "resource_ids": list(item.resource_ids),
                },
            )
        )
    topic_by_id = {topic.id: topic for topic in prepared.topics}
    for item in topics:
        row = topic_by_id.get(item.id)
        entities.append(
            EntityManifestEntry(
                entity_type="topic",
                entity_id=item.id,
                status=item.status,
                updated_at=_stamp(row.updated_at) if row is not None else None,
                selected_fields={
                    "title": title(row.title if row is not None else item.title),
                    "title_key": title(row.title_key if row is not None else item.title_key),
                    "status": item.status,
                    "knowledge_key": item.knowledge_key,
                    "created_at": _stamp_key(row.created_at) if row is not None else "",
                    "resource_ids": list(item.resource_ids),
                },
            )
        )
    resource_by_id = {resource.id: resource for resource in prepared.resources}
    for item in resources:
        row = resource_by_id.get(item.id)
        entities.append(
            EntityManifestEntry(
                entity_type="resource",
                entity_id=item.id,
                status=item.status,
                updated_at=_stamp(row.updated_at) if row is not None else None,
                selected_fields={
                    "title": title(row.title if row is not None else item.title),
                    "status": item.status,
                    # The relative label is content-bearing: it is interpolated into the
                    # excerpt label the model reads, so it belongs in the precondition.
                    # ``knowledge_status`` is deliberately *not* recorded: it is a pure
                    # function of the recorded columns (resource status, current hash,
                    # source status, snapshot status), and re-deriving it here would be a
                    # second implementation of a rule that must have exactly one owner.
                    "relative_path": _truncate(item.relative_path, policy.max_label_chars)[0],
                    "resource_type": item.resource_type,
                    "current_content_hash": item.current_content_hash,
                    "byte_size": item.byte_size,
                    # Shown on the resource and inherited by ``evidence.truncated``, so it
                    # is part of what the model was told.
                    "snapshot_truncated": item.snapshot_truncated,
                    "created_at": _stamp_key(row.created_at) if row is not None else "",
                    "last_seen_at": _stamp_key(row.last_seen_at) if row is not None else "",
                    "source_id": item.source_id,
                    "topic_ids": list(item.topic_ids),
                    "task_ids": list(item.task_ids),
                },
            )
        )
    # The displayed sources *plus* every source a selected resource points at. The
    # latter decide whether a resource's text may be shown as current, so a later
    # apply has to be able to notice that one of them was archived -- even when it
    # did not fit into the ten-source display budget.
    source_entries: dict[str, SourceDescription | ContextSource] = {
        item.id: item for item in sources
    }
    source_entries.update(prepared.referenced_sources)
    # A source that could *not* be used -- deleted, or belonging to another context --
    # decided that a resource's text is not current, so its absence from the manifest
    # would let a later change to it slip through unnoticed.
    source_entries.update(prepared.unusable_sources)
    for source_id, item in source_entries.items():
        row = (
            prepared.source_by_id.get(source_id)
            or prepared.referenced_sources.get(source_id)
            or prepared.unusable_sources.get(source_id)
        )
        if row is None:  # pragma: no cover - the three maps cover every inserted id
            continue
        entities.append(
            EntityManifestEntry(
                entity_type="context_source",
                entity_id=source_id,
                status=row.status,
                updated_at=_stamp(row.updated_at),
                selected_fields={
                    "source_type": title(row.source_type),
                    "status": row.status,
                    # The relative label the packet shows; the absolute path stays out.
                    "label": _truncate(
                        item.label if isinstance(item, SourceDescription) else _source_label(row),
                        policy.max_label_chars,
                    )[0],
                },
            )
        )
    event_by_id = {event.id: event for event in prepared.events}
    for item in events:
        row = event_by_id.get(item.id)
        entities.append(
            EntityManifestEntry(
                entity_type="event",
                entity_id=str(item.id),
                status=None,
                updated_at=None,
                selected_fields={
                    "event_type": _truncate(
                        row.event_type if row is not None else item.event_type,
                        policy.max_event_type_chars,
                    )[0],
                    "occurred_at": _stamp_key(row.occurred_at) if row is not None else "",
                    "entity_type": item.entity_type,
                    "entity_id": item.entity_id,
                    # Only what the packet actually used. A payload that was too big to
                    # materialise was never hashed -- recording a digest anyway would be
                    # a claim the verify side could not keep, and leaving the field out
                    # would hide the event's real content behind a redacted string. The
                    # SQL-measured length is the honest precondition in that case.
                    "payload_length": row.payload_length if row is not None else 0,
                    "payload_hash": (
                        _digest_json(row.payload) if row is not None and row.payload_loaded else ""
                    ),
                    # The read cap this packet used, so the verifier can reproduce the
                    # decision without being told it from outside. See
                    # ``_event_payload_bound`` for why this is an input and not a value
                    # that is compared.
                    "payload_read_cap": policy.max_event_payload_bytes,
                },
            )
        )

    # Two snapshots per resource can matter and both are recorded. ``latest`` is the one
    # whose status the packet reported; ``current`` is the one an excerpt's evidence
    # points at. When the newest snapshot belongs to a different hash these are two
    # different rows, and recording only the newest would leave the snapshot that
    # actually supplied the excerpt outside the apply precondition.
    snapshots: dict[str, SnapshotManifestEntry] = {}
    cited = {excerpt.source_id for excerpt in excerpts if excerpt.kind == "resource_snapshot"}
    for snapshot_id in sorted(cited):
        # An excerpt can survive while its resource row was reduced out of the packet,
        # so the cited snapshot is added explicitly rather than only via the resource.
        row = prepared.snapshot_by_id.get(snapshot_id)
        if row is None:
            continue
        snapshots[snapshot_id] = SnapshotManifestEntry(
            snapshot_id=row.id,
            resource_id=row.resource_id,
            content_hash=row.content_hash,
            extractor_version=row.extractor_version,
            status=row.status,
        )
    for resource in resources:
        for snapshot in (
            prepared.latest_snapshot.get(resource.id),
            prepared.current_snapshot.get(resource.id),
        ):
            if snapshot is None:
                continue
            snapshots.setdefault(
                snapshot.id,
                SnapshotManifestEntry(
                    snapshot_id=snapshot.id,
                    resource_id=resource.id,
                    content_hash=snapshot.content_hash,
                    extractor_version=snapshot.extractor_version,
                    status=snapshot.status,
                ),
            )
    knowledge = [
        KnowledgeManifestEntry(
            context_id=context.id,
            relative_path=excerpt.source_id,
            content_hash=excerpt.evidence.content_hash or "",
        )
        for excerpt in excerpts
        if excerpt.kind == "knowledge"
    ]
    return WorkspaceManifest(
        version=1,
        entities=tuple(entities),
        snapshots=tuple(snapshots.values()),
        knowledge=tuple(knowledge),
    )
