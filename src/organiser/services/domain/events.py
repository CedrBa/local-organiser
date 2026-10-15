"""Audit provenance and event history; writes join the caller transaction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Event,
)


@dataclass(frozen=True)
class EventContext:
    """Optional source, correlation and causation identifiers propagated to audit events."""

    source_type: str | None = None
    source_id: str | None = None
    correlation_id: str | None = None
    causation_event_id: int | None = None


def _serialize(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value


def _changes(**values: tuple[Any, Any]) -> dict[str, dict[str, Any]]:
    return {
        field: {"old": _serialize(old), "new": _serialize(new)}
        for field, (old, new) in values.items()
        if old != new
    }


def record_event(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    context: EventContext | None,
) -> None:
    """Stage a provenance-bearing event in the current transaction without committing."""
    context = context or EventContext()
    session.add(
        Event(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            source_type=context.source_type,
            source_id=context.source_id,
            correlation_id=context.correlation_id,
            causation_event_id=context.causation_event_id,
            payload=payload,
        )
    )


def list_events_for_entity(session: Session, *, entity_type: str, entity_id: str) -> list[Event]:
    """Read audit history for the exact entity type and identifier."""
    return list(
        session.scalars(
            select(Event)
            .where(Event.entity_type == entity_type, Event.entity_id == entity_id)
            .order_by(Event.id)
        )
    )


def list_events_by_correlation_id(session: Session, correlation_id: str) -> list[Event]:
    """Read audit events belonging to one correlation identifier."""
    return list(
        session.scalars(
            select(Event).where(Event.correlation_id == correlation_id).order_by(Event.id)
        )
    )


def list_events_after_id(session: Session, event_id: int) -> list[Event]:
    """Read audit events strictly after the supplied monotonic event identifier."""
    return list(session.scalars(select(Event).where(Event.id > event_id).order_by(Event.id)))
