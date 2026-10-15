"""Task lifecycle, scheduling fields and deterministic queries."""

from __future__ import annotations

from datetime import datetime
from sqlalchemy import Select, or_, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Inbox,
    Task,
    utc_now,
)
from .errors import ConflictError, DomainValidationError, NotFoundError
from .events import EventContext, _changes, record_event
from .queries import _context_or_error, _project_or_error, _task_or_error, _topic_or_error
from .task_bindings import (
    _assert_task_context_change_allowed,
    _resolved_context_id,
    _resolved_topic_id,
    _validate_parent,
)
from .transactions import _commit
from .validation import (
    TERMINAL_TASK_STATUSES,
    UNSET,
    _normalized_time,
    _required_title,
    _validate_schedule,
)


def create_task(
    session: Session,
    *,
    title: str,
    description: str | None = None,
    project_id: str | None = None,
    parent_task_id: str | None = None,
    earliest_start_at: datetime | None = None,
    due_at: datetime | None = None,
    estimated_minutes: int | None = None,
    allow_split: bool = False,
    cognitive_load: str = "medium",
    source_inbox_id: str | None = None,
    context_id: str | None | object = UNSET,
    topic_id: str | None | object = UNSET,
    context: EventContext | None = None,
) -> Task:
    """Create a task after validating schedule, parent and context bindings.

    Commit task and event together, or flush when the enclosing application owns
    the commit. Missing context inherits from a parent; explicit null does not."""
    earliest_start_at, due_at = _normalized_time(earliest_start_at), _normalized_time(due_at)
    _validate_schedule(earliest_start_at, due_at, estimated_minutes)
    if cognitive_load not in {"high", "medium", "low", "physical"}:
        raise DomainValidationError("cognitive_load ist ungueltig")
    if project_id is not None:
        _project_or_error(session, project_id)
    if source_inbox_id is not None and session.get(Inbox, source_inbox_id) is None:
        raise NotFoundError("Inbox-Eintrag nicht gefunden")
    project_id = _validate_parent(
        session, task_id=None, parent_task_id=parent_task_id, project_id=project_id
    )
    resolved_context_id = _resolved_context_id(
        session, context_id=context_id, parent_task_id=parent_task_id
    )
    resolved_topic_id = _resolved_topic_id(
        session, topic_id=None if topic_id is UNSET else topic_id, context_id=resolved_context_id
    )
    task = Task(
        title=_required_title(title),
        description=description,
        status="open",
        project_id=project_id,
        parent_task_id=parent_task_id,
        earliest_start_at=earliest_start_at,
        due_at=due_at,
        estimated_minutes=estimated_minutes,
        allow_split=allow_split,
        cognitive_load=cognitive_load,
        source_inbox_id=source_inbox_id,
        context_id=resolved_context_id,
        topic_id=resolved_topic_id,
    )
    session.add(task)
    session.flush()
    record_event(
        session,
        event_type="TASK_CREATED",
        entity_type="task",
        entity_id=task.id,
        payload={
            "initial": {
                "title": task.title,
                "status": task.status,
                "project_id": task.project_id,
                "parent_task_id": task.parent_task_id,
                "allow_split": task.allow_split,
                "cognitive_load": task.cognitive_load,
                "context_id": task.context_id,
                "topic_id": task.topic_id,
            }
        },
        context=context,
    )
    _commit(session)
    return task


def update_task_details(
    session: Session,
    *,
    task_id: str,
    title: str | object = UNSET,
    description: str | None | object = UNSET,
    estimated_minutes: int | None | object = UNSET,
    allow_split: bool | object = UNSET,
    cognitive_load: str | object = UNSET,
    project_id: str | None | object = UNSET,
    parent_task_id: str | None | object = UNSET,
    context_id: str | None | object = UNSET,
    topic_id: str | None | object = UNSET,
    context: EventContext | None = None,
) -> Task:
    """Update only supplied task fields after validating all affected relationships.

    Reject incompatible parent/context/topic/resource changes before mutation.
    Changes and their event share the commit or deferred-flush boundary."""
    task = _task_or_error(session, task_id)
    if allow_split is not UNSET and not isinstance(allow_split, bool):
        raise DomainValidationError("allow_split muss ein Boolean sein")
    if cognitive_load is not UNSET and cognitive_load not in {"high", "medium", "low", "physical"}:
        raise DomainValidationError("cognitive_load ist ungueltig")
    if estimated_minutes is not UNSET and estimated_minutes is not None and estimated_minutes <= 0:
        raise DomainValidationError("estimated_minutes muss groesser als 0 sein")
    new_project_id = task.project_id if project_id is UNSET else project_id
    new_parent_task_id = task.parent_task_id if parent_task_id is UNSET else parent_task_id
    if new_project_id is not None:
        _project_or_error(session, new_project_id)
    new_project_id = _validate_parent(
        session, task_id=task.id, parent_task_id=new_parent_task_id, project_id=new_project_id
    )
    new_context_id = task.context_id if context_id is UNSET else context_id
    new_topic_id = task.topic_id if topic_id is UNSET else topic_id
    if new_context_id is not None:
        _context_or_error(session, new_context_id)
    # Reihenfolge ist Absicht: erst die Umhaengigkeit des Contextwechsels pruefen,
    # dann Topic und Parent gegen den *neuen* Context validieren.
    _assert_task_context_change_allowed(session, task=task, new_context_id=new_context_id)
    new_topic_id = _resolved_topic_id(session, topic_id=new_topic_id, context_id=new_context_id)
    if new_parent_task_id is not None:
        parent = _task_or_error(session, new_parent_task_id)
        if (
            parent.context_id is not None
            and new_context_id is not None
            and parent.context_id != new_context_id
        ):
            raise ConflictError("Parent und Child muessen demselben Context angehoeren")
        # Ein Reparenting unter einen Parent in einem anderen Context wuerde die
        # bereits vorhandenen Children ueber die Hierarchie trennen. Sie werden
        # nicht automatisch mitgezogen.
        divergent = session.scalar(
            select(Task.id)
            .where(
                Task.parent_task_id == task.id,
                Task.context_id.is_not(None),
                Task.context_id != parent.context_id,
            )
            .limit(1)
        )
        if (
            new_parent_task_id != task.parent_task_id
            and parent.context_id is not None
            and divergent is not None
        ):
            raise ConflictError(
                "Ein Task mit Child-Tasks in einem anderen Context kann nicht unter einen Parent "
                "in einem anderen Context verschoben werden"
            )
    child_with_other_project = session.scalar(
        select(Task.id)
        .where(
            Task.parent_task_id == task.id,
            or_(
                Task.project_id != new_project_id,
                Task.project_id.is_(None)
                if new_project_id is not None
                else Task.project_id.is_not(None),
            ),
        )
        .limit(1)
    )
    if child_with_other_project is not None:
        raise DomainValidationError(
            "Project-Aenderung wuerde Child-Task vom Parent-Project trennen"
        )
    changes = _changes(
        title=(task.title, _required_title(title) if title is not UNSET else task.title),
        description=(
            task.description,
            description if description is not UNSET else task.description,
        ),
        estimated_minutes=(
            task.estimated_minutes,
            estimated_minutes if estimated_minutes is not UNSET else task.estimated_minutes,
        ),
        allow_split=(
            task.allow_split,
            allow_split if allow_split is not UNSET else task.allow_split,
        ),
        cognitive_load=(
            task.cognitive_load,
            cognitive_load if cognitive_load is not UNSET else task.cognitive_load,
        ),
        project_id=(task.project_id, new_project_id),
        parent_task_id=(task.parent_task_id, new_parent_task_id),
        context_id=(task.context_id, new_context_id),
        topic_id=(task.topic_id, new_topic_id),
    )
    if not changes:
        return task
    for field, change in changes.items():
        setattr(task, field, change["new"])
    record_event(
        session,
        event_type="TASK_UPDATED",
        entity_type="task",
        entity_id=task.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return task


def _transition_task(
    session: Session,
    task_id: str,
    *,
    from_statuses: set[str],
    to_status: str,
    event_type: str,
    context: EventContext | None,
) -> Task:
    """Validate the task lifecycle edge and commit state plus event together."""
    task = _task_or_error(session, task_id)
    if task.status not in from_statuses:
        raise ConflictError(f"Task kann nicht von {task.status} nach {to_status} wechseln")
    old_status, old_completed_at = task.status, task.completed_at
    task.status = to_status
    if to_status == "done":
        task.completed_at = utc_now()
    elif to_status == "open":
        task.completed_at = None
    changes = _changes(
        status=(old_status, task.status), completed_at=(old_completed_at, task.completed_at)
    )
    record_event(
        session,
        event_type=event_type,
        entity_type="task",
        entity_id=task.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return task


def start_task(session: Session, *, task_id: str, context: EventContext | None = None) -> Task:
    """Start an eligible task and record the status transition."""
    return _transition_task(
        session,
        task_id,
        from_statuses={"open", "waiting"},
        to_status="in_progress",
        event_type="TASK_STARTED",
        context=context,
    )


def mark_task_waiting(
    session: Session, *, task_id: str, context: EventContext | None = None
) -> Task:
    """Mark an eligible task waiting while preserving its audit history."""
    return _transition_task(
        session,
        task_id,
        from_statuses={"in_progress"},
        to_status="waiting",
        event_type="TASK_WAITING",
        context=context,
    )


def complete_task(session: Session, *, task_id: str, context: EventContext | None = None) -> Task:
    """Complete a task and timestamp it; topic completion is not inferred."""
    return _transition_task(
        session,
        task_id,
        from_statuses={"open", "in_progress", "waiting"},
        to_status="done",
        event_type="TASK_COMPLETED",
        context=context,
    )


def reopen_task(session: Session, *, task_id: str, context: EventContext | None = None) -> Task:
    """Reopen a completed or cancelled task using the lifecycle rules."""
    return _transition_task(
        session,
        task_id,
        from_statuses=TERMINAL_TASK_STATUSES,
        to_status="open",
        event_type="TASK_REOPENED",
        context=context,
    )


def cancel_task(session: Session, *, task_id: str, context: EventContext | None = None) -> Task:
    """Cancel a task without deleting its relationships or history."""
    return _transition_task(
        session,
        task_id,
        from_statuses={"open", "in_progress", "waiting"},
        to_status="cancelled",
        event_type="TASK_CANCELLED",
        context=context,
    )


def reschedule_task(
    session: Session,
    *,
    task_id: str,
    earliest_start_at: datetime | None = None,
    due_at: datetime | None = None,
    context: EventContext | None = None,
) -> Task:
    """Validate aware times and persist schedule changes with an audit event."""
    task = _task_or_error(session, task_id)
    earliest_start_at = (
        _normalized_time(earliest_start_at)
        if earliest_start_at is not None
        else task.earliest_start_at
    )
    due_at = _normalized_time(due_at) if due_at is not None else task.due_at
    _validate_schedule(earliest_start_at, due_at, task.estimated_minutes)
    changes = _changes(
        earliest_start_at=(task.earliest_start_at, earliest_start_at), due_at=(task.due_at, due_at)
    )
    if not changes:
        return task
    task.earliest_start_at, task.due_at = earliest_start_at, due_at
    record_event(
        session,
        event_type="TASK_RESCHEDULED",
        entity_type="task",
        entity_id=task.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return task


def get_task(session: Session, task_id: str) -> Task:
    """Read an existing task or raise NotFoundError."""
    return _task_or_error(session, task_id)


def list_tasks(
    session: Session,
    *,
    status: str | None = None,
    project_id: str | None = None,
    has_deadline: bool | None = None,
    due_from: datetime | None = None,
    due_to: datetime | None = None,
    parent_task_id: str | None = None,
    context_id: str | None = None,
    topic_id: str | None = None,
) -> list[Task]:
    """Read tasks with explicit filters; omitted and explicit-null bindings differ."""
    statement: Select[tuple[Task]] = select(Task).order_by(
        Task.due_at.is_(None), Task.due_at, Task.created_at
    )
    if status is not None:
        statement = statement.where(Task.status == status)
    if project_id is not None:
        statement = statement.where(Task.project_id == project_id)
    if has_deadline is True:
        statement = statement.where(Task.due_at.is_not(None))
    if has_deadline is False:
        statement = statement.where(Task.due_at.is_(None))
    if due_from is not None:
        statement = statement.where(Task.due_at >= _normalized_time(due_from))
    if due_to is not None:
        statement = statement.where(Task.due_at <= _normalized_time(due_to))
    if parent_task_id is not None:
        statement = statement.where(Task.parent_task_id == parent_task_id)
    # Beide Filter sind normale Gleichheitsfilter. Insbesondere loest
    # ``context_id=None`` bewusst *nicht* auf "Tasks ohne Context" auf: das
    # waere eine stille Umkehrung der Bedeutung und wuerde alte Bestandstasks
    # unsichtbar machen.
    if context_id is not None:
        _context_or_error(session, context_id)
        statement = statement.where(Task.context_id == context_id)
    if topic_id is not None:
        _topic_or_error(session, topic_id)
        statement = statement.where(Task.topic_id == topic_id)
    return list(session.scalars(statement))


def list_child_tasks(session: Session, parent_task_id: str) -> list[Task]:
    """Read immediate subtasks of an existing task."""
    _task_or_error(session, parent_task_id)
    return list_tasks(session, parent_task_id=parent_task_id)
