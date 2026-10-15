"""Exact object lookups shared by domain changes; no commits."""

from __future__ import annotations

from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    ContextSource,
    Project,
    Task,
    Topic,
)
from .errors import NotFoundError


def _project_or_error(session: Session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise NotFoundError("Project nicht gefunden")
    return project


def _task_or_error(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise NotFoundError("Task nicht gefunden")
    return task


# -- Task <-> Context/Topic -----------------------------------------------------
#
# Three distinct input states exist for an optional reference and they are not
# interchangeable, which is why the service uses the ``UNSET`` sentinel instead
# of a plain ``None`` default:
#
# * ``UNSET``   -- the caller did not mention the field. On create this inherits
#                  the parent's context exactly once; on update the current value
#                  is kept.
# * ``None``    -- the caller explicitly said "no context" / "no topic". This
#                  never inherits.
# * a real id   -- the caller named the entity.
#
# Collapsing ``UNSET`` into ``None`` at the API edge (a plain ``model_dump()``
# without ``exclude_unset``) would silently turn every omitted field into an
# explicit unlink and every inheritance into a non-inheritance. The router must
# therefore pass ``exclude_unset=True`` for both PATCH and POST. See
# ``docs/architecture/WORKSPACE_TOPICS_TASKS.md``.


def _topic_or_error(session: Session, topic_id: str) -> Topic:
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise NotFoundError("Topic nicht gefunden")
    return topic


def _context_or_error(session: Session, context_id: str) -> Context:
    context = session.get(Context, context_id)
    if context is None:
        raise NotFoundError("Context nicht gefunden")
    return context


def _context_source_or_error(session: Session, *, context_id: str, source_id: str) -> ContextSource:
    source = session.get(ContextSource, source_id)
    if source is None:
        raise NotFoundError("Context-Quelle nicht gefunden")
    if source.context_id != context_id:
        raise NotFoundError("Context-Quelle nicht gefunden")
    return source
