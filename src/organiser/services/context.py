"""Deterministic, bounded context packets for interpreter and planner prompts."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from organiser.db.models import Event, Inbox, Project, Task
from organiser.domain.context import ContextPacket
from organiser.services.domain import NotFoundError, get_project, get_task
from organiser.services.knowledge import KnowledgeStore


MAX_CHILD_TASKS = 10
MAX_RELATED_TASKS = 12
MAX_RECENT_EVENTS = 12
MAX_INBOX_TEXT_LENGTH = 500


def _value(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()
    return value


def _task(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status,
        "project_id": task.project_id,
        "parent_task_id": task.parent_task_id,
        "earliest_start_at": _value(task.earliest_start_at),
        "due_at": _value(task.due_at),
        "estimated_minutes": task.estimated_minutes,
        "completed_at": _value(task.completed_at),
    }


def _project(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "title": project.title,
        "description": project.description,
        "status": project.status,
    }


def _event(event: Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "occurred_at": _value(event.occurred_at),
        "event_type": event.event_type,
        "entity_type": event.entity_type,
        "entity_id": event.entity_id,
        "payload": event.payload,
    }


def _recent_events(
    session: Session, entity_ids: list[str], include_project: bool = False
) -> list[dict[str, Any]]:
    criteria = [Event.entity_type == "task", Event.entity_id.in_(entity_ids)]
    if include_project:
        criteria = [
            or_(Event.entity_type == "project", Event.entity_type == "task"),
            Event.entity_id.in_(entity_ids),
        ]
    events = list(
        session.scalars(
            select(Event).where(*criteria).order_by(Event.id.desc()).limit(MAX_RECENT_EVENTS)
        )
    )
    return [_event(event) for event in reversed(events)]


def _related_tasks(
    session: Session, project_id: str | None, excluded_task_id: str | None = None
) -> list[dict[str, Any]]:
    if project_id is None:
        return []
    statement = select(Task).where(
        Task.project_id == project_id, Task.status.in_(("open", "in_progress", "waiting"))
    )
    if excluded_task_id is not None:
        statement = statement.where(Task.id != excluded_task_id)
    tasks = list(
        session.scalars(
            statement.order_by(Task.due_at.is_(None), Task.due_at, Task.created_at).limit(
                MAX_RELATED_TASKS
            )
        )
    )
    return [_task(task) for task in tasks]


def _source_inbox(session: Session, task: Task) -> dict[str, Any] | None:
    if task.source_inbox_id is None:
        return None
    inbox = session.get(Inbox, task.source_inbox_id)
    if inbox is None:
        return None
    return {
        "id": inbox.id,
        "created_at": _value(inbox.created_at),
        "text": inbox.text[:MAX_INBOX_TEXT_LENGTH],
    }


def _event_max_id(session: Session) -> int:
    return session.scalar(select(func.max(Event.id))) or 0


def _workspace_excerpt(session: Session, task: Task, store: KnowledgeStore | None) -> Any:
    """Small workspace excerpt for a task that is bound to a context.

    Only ``task.context_id`` is consulted. A project is never used to guess a
    context: a task without an explicit context simply gets no workspace, because a
    guessed course context would put the wrong documents in front of the interpreter
    and the planner.

    ``store`` is imported lazily so this module keeps no import-time dependency on
    the knowledge store, and it may be ``None`` in which case no excerpt is added at
    all. A missing store therefore cannot add workspace *content* to an existing
    packet; the serialised form still carries one nullable ``workspace`` key.
    """
    if store is None or task.context_id is None:
        return None
    from organiser.services.workspace_context import workspace_excerpt_summary

    return workspace_excerpt_summary(session, task.context_id, store)


def task_context(
    session: Session, task_id: str, *, store: KnowledgeStore | None = None
) -> ContextPacket:
    """Build the bounded derived context of an existing task without changing state."""
    task = get_task(session, task_id)
    parent = get_task(session, task.parent_task_id) if task.parent_task_id else None
    children = list(
        session.scalars(
            select(Task)
            .where(Task.parent_task_id == task.id)
            .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at)
            .limit(MAX_CHILD_TASKS)
        )
    )
    project = get_project(session, task.project_id) if task.project_id else None
    entity_ids = [task.id, *(child.id for child in children)]
    if parent:
        entity_ids.append(parent.id)
    return ContextPacket(
        kind="task_context",
        target_type="task",
        target_id=task.id,
        context_event_max_id=_event_max_id(session),
        target=_task(task),
        project=_project(project) if project else None,
        parent_task=_parent_task(parent) if parent else None,
        child_tasks=[_task(child) for child in children],
        related_tasks=_related_tasks(session, task.project_id, task.id),
        recent_events=_recent_events(session, entity_ids),
        source_inbox=_source_inbox(session, task),
        selection={
            "max_child_tasks": MAX_CHILD_TASKS,
            "max_related_tasks": MAX_RELATED_TASKS,
            "max_recent_events": MAX_RECENT_EVENTS,
        },
        workspace=_workspace_excerpt(session, task, store),
    )


def project_context(session: Session, project_id: str) -> ContextPacket:
    """Build the bounded derived context of an existing project without changing state."""
    project = get_project(session, project_id)
    tasks = list(
        session.scalars(
            select(Task)
            .where(Task.project_id == project.id)
            .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at)
            .limit(MAX_RELATED_TASKS)
        )
    )
    event_ids = [project.id, *(task.id for task in tasks)]
    events = list(
        session.scalars(
            select(Event)
            .where(
                or_(Event.entity_type == "project", Event.entity_type == "task"),
                Event.entity_id.in_(event_ids),
            )
            .order_by(Event.id.desc())
            .limit(MAX_RECENT_EVENTS)
        )
    )
    return ContextPacket(
        kind="project_context",
        target_type="project",
        target_id=project.id,
        context_event_max_id=_event_max_id(session),
        target=_project(project),
        related_tasks=[_task(task) for task in tasks],
        recent_events=[_event(event) for event in reversed(events)],
        selection={
            "max_child_tasks": MAX_CHILD_TASKS,
            "max_related_tasks": MAX_RELATED_TASKS,
            "max_recent_events": MAX_RECENT_EVENTS,
        },
    )


def _parent_task(parent: Task) -> dict[str, Any]:
    """The parent task, including its own context/topic binding.

    A parent may be bound to a context while the child is not, so the binding is
    part of what a caller can see here -- without it, a child packet would silently
    lose the context of the work it belongs to.

    This is a deliberate, unconditional change to the ``parent_task`` shape, not a
    workspace-only addition: two extra keys, both additive and independent of any
    knowledge store.
    """
    return {**_task(parent), "context_id": parent.context_id, "topic_id": parent.topic_id}


def planning_context(
    session: Session,
    *,
    target_type: str,
    target_id: str,
    store: KnowledgeStore | None = None,
) -> ContextPacket:
    """Select the explicit task/project planning view; do not infer a different scope."""
    if target_type == "task":
        packet = task_context(session, target_id, store=store)
    elif target_type == "project":
        packet = project_context(session, target_id)
    else:
        raise NotFoundError("Planning-Kontext benoetigt task oder project als Ziel")
    return packet.model_copy(update={"kind": "planning_context"})
