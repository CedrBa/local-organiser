"""Exact topic/context/resource/task scope checks and knowledge bindings."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    Resource,
    Task,
    Topic,
)
from organiser.domain.topics import (
    TopicInputError,
)
from organiser.services.domain import (
    DomainValidationError,
    NotFoundError,
)


def _topic_input(error: TopicInputError) -> DomainValidationError:
    return DomainValidationError(str(error))


def get_topic(session: Session, topic_id: str) -> Topic:
    """Load a topic or raise :class:`NotFoundError`."""
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise NotFoundError("Topic nicht gefunden")
    return topic


def _topic_or_error(session: Session, topic_id: str) -> Topic:
    return get_topic(session, topic_id)


def _context_or_error(session: Session, context_id: str) -> Context:
    context = session.get(Context, context_id)
    if context is None:
        raise NotFoundError("Context nicht gefunden")
    return context


def topic_knowledge_binding(session: Session, topic_id: str):
    """Return the :class:`~organiser.domain.topics.TopicBinding` of a topic.

    Read as a column query rather than through the identity map: the knowledge
    write needs the committed key and title, and a long-lived session could
    otherwise hand out a stale ``title`` after a rename. The row is intentionally
    not in the identity map, so this read cannot itself modify the topic.
    """
    row = session.execute(
        select(Topic.id, Topic.context_id, Topic.title, Topic.knowledge_key).where(
            Topic.id == topic_id
        )
    ).one_or_none()
    if row is None:
        raise NotFoundError("Topic nicht gefunden")
    from organiser.domain.topics import TopicBinding

    return TopicBinding(
        topic_id=row.id, context_id=row.context_id, title=row.title, knowledge_key=row.knowledge_key
    )


# -- resource <-> topic -------------------------------------------------------


def _resource_or_error(session: Session, resource_id: str) -> Resource:
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise NotFoundError("Resource nicht gefunden")
    return resource


# -- task <-> resource --------------------------------------------------------


def _task_or_error(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise NotFoundError("Task nicht gefunden")
    return task
