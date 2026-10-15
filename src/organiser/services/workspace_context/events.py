"""Bounded event queries that avoid materialising unrestricted JSON payloads."""

from __future__ import annotations

from typing import Any, Sequence
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    Event,
)
from .policy import WorkspacePolicy
from .state import _EventRow


# ---------------------------------------------------------------------------
# DB preparation -- every query carries a LIMIT or is a COUNT
# ---------------------------------------------------------------------------


def _count(session: Session, model: type, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return int(session.scalar(statement) or 0)


def _event_clauses(
    context: Context, resource_ids: Sequence[str], task_ids: Sequence[str], topic_ids: Sequence[str]
) -> list[Any]:
    """One AND-closed clause per entity kind, to be OR-ed together.

    Each clause must be an ``and_`` of type *and* id. An ``or_`` here would silently
    widen the packet to every event of that kind in the whole database -- including
    the ones belonging to child contexts, which is exactly the unrequested
    aggregation this model must not perform.
    """
    clauses: list[Any] = [and_(Event.entity_type == "context", Event.entity_id == context.id)]
    for entity_type, ids in (
        ("task", list(task_ids)),
        ("topic", list(topic_ids)),
        ("resource", list(resource_ids)),
    ):
        if ids:
            clauses.append(and_(Event.entity_type == entity_type, Event.entity_id.in_(ids)))
    return clauses


def _event_payload_expression(limit: int) -> Any:
    """SQL expression that yields the payload only when it is small enough.

    ``CASE WHEN length(payload) <= :limit THEN payload ELSE NULL END``. The size is
    decided by SQLite, so an oversized legacy blob is never handed to the JSON layer:
    one GET cannot turn into a deserialisation of megabytes because a writer once
    stored a large payload. The measured length is selected next to it either way.
    """
    return case((func.length(Event.payload) <= max(0, limit), Event.payload))


def _event_payload_columns(limit: int) -> tuple[Any, ...]:
    """The event columns every bounded event read selects.

    Four facts about the payload, all decided by SQLite, none of them by what the JSON
    layer happens to decode:

    * ``bounded_payload`` -- the payload only while its SQL-measured length is within
      ``limit``. Above that the blob never reaches Python.
    * ``payload_length`` -- the measurement, so the manifest can record what was used.
    * ``payload_within_bound`` -- the budget decision itself, as a column.
    * ``payload_is_absent`` -- a SQL ``NULL``, which is "there is no payload at all".

    The decision must be a column rather than something derived from the decoded value,
    because a stored JSON ``null`` is four characters long and decodes to ``None``: a
    rule of the form "decoded to ``None``, therefore not loaded" reports a payload that
    was in fact loaded within the budget as withheld, and hashes nothing for it.
    """
    return (
        _event_payload_expression(limit).label("bounded_payload"),
        func.coalesce(func.length(Event.payload), 0).label("payload_length"),
        case((func.length(Event.payload) <= limit, True), else_=False).label(
            "payload_within_bound"
        ),
        Event.payload.is_(None).label("payload_is_absent"),
    )


def _event_row_from_columns(row: Any, limit: int) -> _EventRow:
    """Build one :class:`_EventRow` from the columns of :func:`_event_payload_columns`."""
    length = int(row.payload_length or 0)
    return _EventRow(
        id=int(row.id),
        occurred_at=row.occurred_at,
        event_type=row.event_type,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        payload=row.bounded_payload,
        payload_length=length,
        # The SQL decision, independent of what the JSON layer decoded. A payload that
        # was within the budget was loaded, even when it decodes to ``None``.
        payload_loaded=bool(row.payload_within_bound) or bool(row.payload_is_absent),
        payload_limit=limit,
    )


def _load_event_row(session: Session, event_id: int, limit: int) -> _EventRow | None:
    """One event, read fresh and bounded, for the manifest verifier.

    A *column* query, deliberately not ``session.get(Event, ...)``. The ORM path would
    load the whole ``payload`` column -- the one column without a size constraint -- and
    would hand back whatever the identity map happened to cache from an earlier build in
    the same session, so the new bound would be bypassed by the very request that was
    supposed to prove it. A column query never consults the identity map and never
    defers a lazy load, so nothing can pull the blob behind our back.
    """
    row = session.execute(
        select(
            Event.id,
            Event.occurred_at,
            Event.event_type,
            Event.entity_type,
            Event.entity_id,
            *_event_payload_columns(limit),
        ).where(Event.id == event_id)
    ).first()
    return None if row is None else _event_row_from_columns(row, limit)


def _recent_events(
    session: Session,
    context: Context,
    resource_ids: Sequence[str],
    task_ids: Sequence[str],
    topic_ids: Sequence[str],
    policy: WorkspacePolicy,
) -> list[_EventRow]:
    """Newest events of exactly the entities this packet talks about, oldest first.

    Chronological order matches the existing context packets. The entity set is
    closed: the context itself plus the selected tasks, topics and resources. No
    unrequested aggregation.

    Rows are read as columns, not as ORM entities, for two reasons: the identity map
    would hand back a previously loaded full ``Event`` with its whole payload, and the
    payload is the one column in this schema with no size constraint. Only the
    selected columns are fetched and the payload is gated by its SQL-measured length.
    """
    limit = policy.max_event_payload_bytes
    rows = list(
        session.execute(
            select(
                Event.id,
                Event.occurred_at,
                Event.event_type,
                Event.entity_type,
                Event.entity_id,
                *_event_payload_columns(limit),
            )
            .where(or_(*_event_clauses(context, resource_ids, task_ids, topic_ids)))
            .order_by(Event.id.desc())
            .limit(policy.max_events)
        ).all()
    )
    return [_event_row_from_columns(row, limit) for row in reversed(rows)]


def _count_events(
    session: Session,
    context: Context,
    resource_ids: Sequence[str],
    task_ids: Sequence[str],
    topic_ids: Sequence[str],
) -> int:
    return _count(session, Event, or_(*_event_clauses(context, resource_ids, task_ids, topic_ids)))
