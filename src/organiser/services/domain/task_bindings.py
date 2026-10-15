"""Task hierarchy and context/topic relationship validation before mutation."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Task,
    TaskResource,
)
from .errors import ConflictError, DomainValidationError
from .queries import _context_or_error, _task_or_error, _topic_or_error
from .validation import UNSET


def _validate_parent(
    session: Session, *, task_id: str | None, parent_task_id: str | None, project_id: str | None
) -> str | None:
    if parent_task_id is None:
        return project_id
    if parent_task_id == task_id:
        raise DomainValidationError("Ein Task kann nicht sein eigener Parent sein")
    parent = _task_or_error(session, parent_task_id)
    cursor = parent
    while cursor.parent_task_id is not None:
        if cursor.parent_task_id == task_id:
            raise DomainValidationError("Task-Hierarchie darf keinen Zyklus enthalten")
        cursor = _task_or_error(session, cursor.parent_task_id)
    if project_id is None and parent.project_id is not None:
        return parent.project_id
    if project_id != parent.project_id:
        raise DomainValidationError("Child und Parent muessen demselben Project angehoeren")
    return project_id


def _resolved_context_id(
    session: Session, *, context_id: str | None | object, parent_task_id: str | None
) -> str | None:
    """Determine the context a task is created in.

    An omitted ``context_id`` takes over the parent's context **once**. An
    explicit ``None`` does not, and a child without a parent simply has no
    context. If both parent and child end up with a context, the two must be
    equal -- otherwise the pair would be ambiguous by construction.
    """
    if context_id is UNSET:
        inherited = (
            parent_task_id is not None and _task_or_error(session, parent_task_id).context_id
        )
        effective = inherited or None
    else:
        effective = context_id
    if effective is not None:
        _context_or_error(session, effective)
        if parent_task_id is not None:
            parent = _task_or_error(session, parent_task_id)
            if parent.context_id is not None and parent.context_id != effective:
                raise ConflictError("Parent und Child muessen demselben Context angehoeren")
    return effective


def _resolved_topic_id(
    session: Session, *, topic_id: str | None, context_id: str | None
) -> str | None:
    """A topic may only be referenced from within the topic's own context."""
    if topic_id is None:
        return None
    topic = _topic_or_error(session, topic_id)
    if context_id is None:
        raise ConflictError("Ein Topic- Bezug setzt einen Context voraus")
    if topic.context_id != context_id:
        raise ConflictError("Topic und Task muessen demselben Context angehoeren")
    return topic.id


def _assert_task_context_change_allowed(
    session: Session, *, task: Task, new_context_id: str | None
) -> None:
    """Refuse a context change that would strand an existing reference.

    Reparenting a task to another context is legitimate; doing so while the task
    still carries topic, resource or child references would leave those
    references pointing across contexts. Nothing is reassigned automatically --
    the caller must unlink explicitly first, so the consequence of a context
    change stays visible in the audit log instead of happening behind the
    user's back.
    """
    if new_context_id == task.context_id:
        return
    if task.topic_id is not None:
        raise ConflictError(
            "Der Context eines Tasks mit Topic-Bezug kann nicht geaendert werden; "
            "bitte den Topic-Bezug zuerst explizit entfernen"
        )
    linked_resource = session.scalar(
        select(TaskResource.resource_id).where(TaskResource.task_id == task.id).limit(1)
    )
    if linked_resource is not None:
        raise ConflictError(
            "Der Context eines Tasks mit Resource-Bezuegen kann nicht geaendert werden; "
            "bitte die Resource-Bezuege zuerst explizit entfernen"
        )
    divergent_child = session.scalar(
        select(Task.id)
        .where(
            Task.parent_task_id == task.id,
            Task.context_id.is_not(None),
            Task.context_id != new_context_id,
        )
        .limit(1)
    )
    if divergent_child is not None:
        raise ConflictError(
            "Der Context eines Tasks mit Child-Tasks in einem anderen Context kann nicht "
            "geaendert werden; bitte die Child-Tasks zuerst explizit verschieben"
        )
