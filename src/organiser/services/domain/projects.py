"""Project lifecycle and status transitions with matching audit events."""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Project,
    Task,
)
from .errors import ConflictError
from .events import EventContext, _changes, record_event
from .queries import _project_or_error
from .tasks import list_tasks
from .transactions import _commit
from .validation import TERMINAL_TASK_STATUSES, UNSET, _required_title


def create_project(
    session: Session,
    *,
    title: str,
    description: str | None = None,
    context: EventContext | None = None,
) -> Project:
    """Create an active project and audit event; commit or flush with the caller policy."""
    project = Project(title=_required_title(title), description=description, status="active")
    session.add(project)
    session.flush()
    record_event(
        session,
        event_type="PROJECT_CREATED",
        entity_type="project",
        entity_id=project.id,
        payload={
            "initial": {
                "title": project.title,
                "description": project.description,
                "status": project.status,
            }
        },
        context=context,
    )
    _commit(session)
    return project


def update_project(
    session: Session,
    *,
    project_id: str,
    title: str | object = UNSET,
    description: str | None | object = UNSET,
    context: EventContext | None = None,
) -> Project:
    """Update supplied fields and audit their changes; unchanged values are a no-op."""
    project = _project_or_error(session, project_id)
    changes = _changes(
        title=(project.title, _required_title(title) if title is not UNSET else project.title),
        description=(
            project.description,
            description if description is not UNSET else project.description,
        ),
    )
    if not changes:
        return project
    for field, change in changes.items():
        setattr(project, field, change["new"])
    record_event(
        session,
        event_type="PROJECT_UPDATED",
        entity_type="project",
        entity_id=project.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return project


def _transition_project(
    session: Session,
    project_id: str,
    *,
    from_statuses: set[str],
    to_status: str,
    event_type: str,
    context: EventContext | None,
) -> Project:
    """Validate the project lifecycle edge and commit state plus event together."""
    project = _project_or_error(session, project_id)
    if project.status not in from_statuses:
        raise ConflictError(f"Project kann nicht von {project.status} nach {to_status} wechseln")
    if to_status == "completed":
        open_task = session.scalar(
            select(Task.id)
            .where(Task.project_id == project.id, Task.status.not_in(TERMINAL_TASK_STATUSES))
            .limit(1)
        )
        if open_task is not None:
            raise ConflictError(
                "Project mit nicht-terminalen Tasks kann nicht abgeschlossen werden"
            )
    old_status = project.status
    project.status = to_status
    record_event(
        session,
        event_type=event_type,
        entity_type="project",
        entity_id=project.id,
        payload={"changes": _changes(status=(old_status, to_status))},
        context=context,
    )
    _commit(session)
    return project


def pause_project(
    session: Session, *, project_id: str, context: EventContext | None = None
) -> Project:
    """Pause an eligible project and persist its status event."""
    return _transition_project(
        session,
        project_id,
        from_statuses={"active"},
        to_status="on_hold",
        event_type="PROJECT_PAUSED",
        context=context,
    )


def resume_project(
    session: Session, *, project_id: str, context: EventContext | None = None
) -> Project:
    """Resume a paused project; reject a transition outside the lifecycle."""
    return _transition_project(
        session,
        project_id,
        from_statuses={"on_hold"},
        to_status="active",
        event_type="PROJECT_RESUMED",
        context=context,
    )


def complete_project(
    session: Session, *, project_id: str, context: EventContext | None = None
) -> Project:
    """Complete a project through the validated lifecycle transition."""
    return _transition_project(
        session,
        project_id,
        from_statuses={"active", "on_hold"},
        to_status="completed",
        event_type="PROJECT_COMPLETED",
        context=context,
    )


def cancel_project(
    session: Session, *, project_id: str, context: EventContext | None = None
) -> Project:
    """Cancel a project without deleting its data or audit history."""
    return _transition_project(
        session,
        project_id,
        from_statuses={"active", "on_hold"},
        to_status="cancelled",
        event_type="PROJECT_CANCELLED",
        context=context,
    )


def reopen_project(
    session: Session, *, project_id: str, context: EventContext | None = None
) -> Project:
    """Reopen a terminal project through the supported status transition."""
    return _transition_project(
        session,
        project_id,
        from_statuses={"completed", "cancelled"},
        to_status="active",
        event_type="PROJECT_REOPENED",
        context=context,
    )


def get_project(session: Session, project_id: str) -> Project:
    """Read an existing project or raise NotFoundError."""
    return _project_or_error(session, project_id)


def list_projects(session: Session, status: str | None = None) -> list[Project]:
    """Read projects, optionally filtered by lifecycle status."""
    statement: Select[tuple[Project]] = select(Project).order_by(Project.created_at, Project.id)
    if status is not None:
        statement = statement.where(Project.status == status)
    return list(session.scalars(statement))


def list_project_tasks(session: Session, project_id: str) -> list[Task]:
    """Read tasks of an existing project without aggregating unrelated contexts."""
    _project_or_error(session, project_id)
    return list_tasks(session, project_id=project_id)
