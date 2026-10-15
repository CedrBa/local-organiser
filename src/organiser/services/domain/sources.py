"""Source registration and lifecycle; registration grants no file access."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    ContextSource,
)
from .errors import ConflictError, DomainValidationError
from .events import EventContext, _changes, record_event
from .queries import _context_or_error, _context_source_or_error
from .transactions import _commit
from .validation import CONTEXT_SOURCE_STATUSES, CONTEXT_SOURCE_TYPES, UNSET, _required_path


def _validate_source_type(source_type: str) -> str:
    normalized = source_type.strip()
    if normalized not in CONTEXT_SOURCE_TYPES:
        raise DomainValidationError("source_type ist ungueltig")
    return normalized


def _validate_source_status(status: str) -> str:
    normalized = status.strip()
    if normalized not in CONTEXT_SOURCE_STATUSES:
        raise DomainValidationError("source status ist ungueltig")
    return normalized


def get_context_source(session: Session, *, context_id: str, source_id: str) -> ContextSource:
    """Read a registered source belonging to the requested context or refuse it."""
    _context_or_error(session, context_id)
    return _context_source_or_error(session, context_id=context_id, source_id=source_id)


def list_context_sources(
    session: Session, *, context_id: str, status: str | None = None
) -> list[ContextSource]:
    """List registrations for an existing context; grant no filesystem permissions."""
    _context_or_error(session, context_id)
    statement = (
        select(ContextSource)
        .where(ContextSource.context_id == context_id)
        .order_by(ContextSource.created_at, ContextSource.id)
    )
    if status is not None:
        statement = statement.where(ContextSource.status == _validate_source_status(status))
    return list(session.scalars(statement))


def add_context_source(
    session: Session,
    *,
    context_id: str,
    source_type: str,
    path: str,
    status: str = "active",
    context: EventContext | None = None,
) -> ContextSource:
    """Register a source and event after validation; registration does not read files."""
    existing = _context_or_error(session, context_id)
    source_type, path, status = (
        _validate_source_type(source_type),
        _required_path(path),
        _validate_source_status(status),
    )
    duplicate = session.scalar(
        select(ContextSource.id)
        .where(ContextSource.context_id == existing.id, ContextSource.path == path)
        .limit(1)
    )
    if duplicate is not None:
        raise ConflictError("Diese Quelle ist im Context bereits registriert")
    source = ContextSource(
        context_id=existing.id, source_type=source_type, path=path, status=status
    )
    session.add(source)
    session.flush()
    record_event(
        session,
        event_type="CONTEXT_SOURCE_ADDED",
        entity_type="context_source",
        entity_id=source.id,
        payload={
            "initial": {
                "context_id": source.context_id,
                "source_type": source.source_type,
                "path": source.path,
                "status": source.status,
            }
        },
        context=context,
    )
    _commit(session)
    return source


def update_context_source(
    session: Session,
    *,
    context_id: str,
    source_id: str,
    source_type: str | object = UNSET,
    path: str | object = UNSET,
    status: str | object = UNSET,
    context: EventContext | None = None,
) -> ContextSource:
    """Update a context-owned source registration with an atomic audit event."""
    existing_context = _context_or_error(session, context_id)
    source = _context_source_or_error(session, context_id=context_id, source_id=source_id)
    new_path = _required_path(path) if path is not UNSET else source.path
    if new_path != source.path and session.scalar(
        select(ContextSource.id)
        .where(
            ContextSource.context_id == existing_context.id,
            ContextSource.path == new_path,
            ContextSource.id != source.id,
        )
        .limit(1)
    ):
        raise ConflictError("Diese Quelle ist im Context bereits registriert")
    changes = _changes(
        source_type=(
            source.source_type,
            _validate_source_type(source_type) if source_type is not UNSET else source.source_type,
        ),
        path=(source.path, new_path),
        status=(
            source.status,
            _validate_source_status(status) if status is not UNSET else source.status,
        ),
    )
    if not changes:
        return source
    for field, change in changes.items():
        setattr(source, field, change["new"])
    record_event(
        session,
        event_type="CONTEXT_SOURCE_UPDATED",
        entity_type="context_source",
        entity_id=source.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return source


def remove_context_source(
    session: Session, *, context_id: str, source_id: str, context: EventContext | None = None
) -> None:
    """Remove a registration and record its previous fields; never delete its files."""
    _context_or_error(session, context_id)
    source = _context_source_or_error(session, context_id=context_id, source_id=source_id)
    removed = {
        "context_id": source.context_id,
        "source_type": source.source_type,
        "path": source.path,
    }
    session.delete(source)
    record_event(
        session,
        event_type="CONTEXT_SOURCE_REMOVED",
        entity_type="context_source",
        entity_id=source_id,
        payload={"removed": removed},
        context=context,
    )
    _commit(session)
