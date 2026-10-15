"""Bounded resource and snapshot read views with explicit staleness."""

from __future__ import annotations

from typing import Sequence
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    Resource,
    ResourceSnapshot,
)
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    ContextNotFoundError,
    IngestionLimits,
    ResourceDetail,
    ResourceIngestionError,
    ResourceListPage,
    ResourceNotFoundError,
    ResourceSummary,
    SnapshotDetail,
    SnapshotStatus,
    SnapshotSummary,
)
from .constants import (
    CURRENT_SNAPSHOT_STATUSES,
    REASON_STALE_ARCHIVED,
    REASON_STALE_MISSING,
    REASON_STALE_NO_SNAPSHOT,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_MISSING,
)


# -- read-only queries -------------------------------------------------------


def _current_snapshot_ids(session: Session, resource_ids: Sequence[str]) -> dict[str, str]:
    """Map resource id to its current snapshot id, in one query."""
    if not resource_ids:
        return {}
    # A snapshot is current only if it is ready *and* carries the resource's
    # current content hash. The hash is part of the join, so an older ready
    # snapshot of the same bytes is still found instead of being reported as
    # "no snapshot".
    rows = session.execute(
        select(Resource.id, ResourceSnapshot.id)
        .join(
            ResourceSnapshot,
            (ResourceSnapshot.resource_id == Resource.id)
            & (ResourceSnapshot.content_hash == Resource.current_content_hash)
            & (ResourceSnapshot.status.in_(CURRENT_SNAPSHOT_STATUSES)),
        )
        .where(Resource.id.in_(resource_ids))
        .order_by(Resource.id, ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc())
    )
    mapping: dict[str, str] = {}
    for resource_id, snapshot_id in rows:
        mapping.setdefault(resource_id, snapshot_id)
    return mapping


def _summarize(resource: Resource, *, current_snapshot_id: str | None = None) -> ResourceSummary:
    return ResourceSummary(
        id=resource.id,
        context_id=resource.context_id,
        source_id=resource.source_id,
        title=resource.title,
        relative_path=resource.relative_path,
        resource_type=resource.resource_type,
        status=resource.status,
        has_content_hash=resource.current_content_hash is not None,
        byte_size=resource.byte_size,
        mtime_ns=resource.mtime_ns,
        current_snapshot_id=current_snapshot_id,
        last_seen_at=resource.last_seen_at,
        updated_at=resource.updated_at,
    )


def list_resources(
    session: Session,
    context_id: str,
    *,
    source_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
    limits: IngestionLimits = DEFAULT_LIMITS,
) -> ResourceListPage:
    """List the resources of one context, most recently observed first.

    The page is bounded and deterministic. ``limit`` is clamped to the fixed
    maximum of the component, so a client cannot ask for the whole table, and
    ``offset`` cannot be negative. The context must exist: the empty inventory of
    an unknown context is an error, not an empty page.
    """
    if session.get(Context, context_id) is None:
        raise ContextNotFoundError("Der Context existiert nicht", context_id=context_id)
    bounded_limit = max(1, min(int(limit), limits.max_resource_rows))
    bounded_offset = max(0, int(offset))
    conditions = [Resource.context_id == context_id]
    if source_id is not None:
        conditions.append(Resource.source_id == source_id)
    if status is not None:
        if status not in (STATUS_ACTIVE, STATUS_MISSING, STATUS_ARCHIVED):
            raise ResourceIngestionError(
                "Unbekannter Ressourcenstatus", code="invalid_status", status=status
            )
        conditions.append(Resource.status == status)
    total = session.scalar(select(func.count(Resource.id)).where(*conditions)) or 0
    resources = list(
        session.scalars(
            select(Resource)
            .where(*conditions)
            .order_by(Resource.last_seen_at.desc(), Resource.id)
            .limit(bounded_limit)
            .offset(bounded_offset)
        )
    )
    current = _current_snapshot_ids(session, [resource.id for resource in resources])
    return ResourceListPage(
        items=tuple(
            _summarize(resource, current_snapshot_id=current.get(resource.id))
            for resource in resources
        ),
        total=int(total),
        limit=bounded_limit,
        offset=bounded_offset,
    )


def _snapshot_summary(snapshot: ResourceSnapshot) -> SnapshotSummary:
    return SnapshotSummary(
        id=snapshot.id,
        resource_id=snapshot.resource_id,
        content_hash=snapshot.content_hash,
        extractor_version=snapshot.extractor_version,
        status=SnapshotStatus(snapshot.status),
        truncated=snapshot.truncated,
        error_code=snapshot.error_code,
        character_count=len(snapshot.text) if snapshot.text is not None else None,
        page_count=(snapshot.metadata_payload or {}).get("page_count"),
        created_at=snapshot.created_at,
    )


def get_resource_snapshot(
    session: Session, resource_id: str, *, limits: IngestionLimits = DEFAULT_LIMITS
) -> ResourceDetail:
    """Return one resource, its snapshot history and an explicit staleness verdict.

    ``current_snapshot`` is ``None`` whenever no ready snapshot describes the
    resource's current version. That is the purpose of the function: a caller
    that wants to quote a document can see that the quote is from an older
    version without comparing hashes itself.
    """
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise ResourceNotFoundError("Die Ressource existiert nicht", resource_id=resource_id)
    # The history is bounded: at most `max_snapshot_rows` rows are materialised,
    # and the total is counted, so a long-lived registry cannot answer an
    # unbounded query.
    total_snapshots = int(
        session.scalar(
            select(func.count(ResourceSnapshot.id)).where(
                ResourceSnapshot.resource_id == resource_id
            )
        )
        or 0
    )
    snapshots = list(
        session.scalars(
            select(ResourceSnapshot)
            .where(ResourceSnapshot.resource_id == resource_id)
            .order_by(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc())
            .limit(limits.max_snapshot_rows)
        )
    )
    current_id = _current_snapshot_ids(session, [resource_id]).get(resource_id)
    current = next((item for item in snapshots if item.id == current_id), None)
    stale_reason: str | None = None
    if resource.status == STATUS_MISSING:
        stale_reason = REASON_STALE_MISSING
    elif resource.status == STATUS_ARCHIVED:
        stale_reason = REASON_STALE_ARCHIVED
    elif current is None:
        stale_reason = REASON_STALE_NO_SNAPSHOT
    detail = None
    if current is not None:
        detail = SnapshotDetail(
            **_snapshot_summary(current).model_dump(),
            text=current.text,
            metadata_payload=current.metadata_payload or {},
        )
    return ResourceDetail(
        resource=_summarize(resource, current_snapshot_id=current_id),
        current_snapshot=detail,
        snapshots=tuple(_snapshot_summary(item) for item in snapshots),
        snapshot_is_stale=stale_reason is not None,
        stale_reason=stale_reason,
        snapshot_count=total_snapshots,
        snapshots_truncated=total_snapshots > limits.max_snapshot_rows,
    )
