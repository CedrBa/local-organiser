"""Shared field and lifecycle vocabulary without IO."""

from __future__ import annotations

from datetime import UTC, datetime
from organiser.domain.knowledge_path import (
    MAX_KNOWLEDGE_PATH_LENGTH,
)
from .errors import DomainValidationError


TERMINAL_TASK_STATUSES = {"done", "cancelled"}


UNSET = object()


def _required_title(title: str) -> str:
    if not isinstance(title, str):
        raise DomainValidationError("title darf nicht leer sein")
    normalized = title.strip()
    if not normalized:
        raise DomainValidationError("title darf nicht leer sein")
    return normalized


def _normalized_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise DomainValidationError("Zeitstempel muessen eine Zeitzone enthalten")
    return value.astimezone(UTC)


def _validate_schedule(
    earliest_start_at: datetime | None, due_at: datetime | None, estimated_minutes: int | None
) -> None:
    if estimated_minutes is not None and estimated_minutes <= 0:
        raise DomainValidationError("estimated_minutes muss groesser als 0 sein")
    if earliest_start_at is not None and due_at is not None and earliest_start_at > due_at:
        raise DomainValidationError("earliest_start_at darf nicht nach due_at liegen")


CONTEXT_STATUSES = ("active", "on_hold", "completed", "cancelled")


CONTEXT_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "active": {"on_hold", "completed", "cancelled"},
    "on_hold": {"active", "completed", "cancelled"},
    "completed": {"active"},
    "cancelled": {"active"},
}


CONTEXT_SOURCE_TYPES = ("manual", "document", "folder", "note", "link", "inbox")


CONTEXT_SOURCE_STATUSES = ("active", "missing", "archived")


def _required_path(path: str) -> str:
    if not isinstance(path, str):
        raise DomainValidationError("path darf nicht leer sein")
    normalized = path.strip()
    if not normalized:
        raise DomainValidationError("path darf nicht leer sein")
    return normalized


# Re-exportiert, damit die bisherige Konstante stabil importierbar bleibt.
KNOWLEDGE_PATH_MAX_LENGTH = MAX_KNOWLEDGE_PATH_LENGTH
