"""Deterministic topic and linked-resource views without knowledge writes."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Resource,
    ResourceTopic,
    TaskResource,
    Topic,
)
from organiser.domain.topics import (
    TopicInputError,
    validate_topic_status,
)
from .bindings import _context_or_error, _task_or_error, _topic_input, _topic_or_error


def list_topics(session: Session, context_id: str, *, status: str | None = None) -> list[Topic]:
    """List a context's topics, ordered by title key then id (the workspace order).

    Ordering by ``title_key`` and not by ``title`` is what makes the listing
    deterministic regardless of the display spelling or of German case folding.
    """
    _context_or_error(session, context_id)
    statement = select(Topic).where(Topic.context_id == context_id)
    if status is not None:
        try:
            status = validate_topic_status(status)
        except TopicInputError as error:
            raise _topic_input(error) from error
        statement = statement.where(Topic.status == status)
    return list(session.scalars(statement.order_by(Topic.title_key, Topic.id)))


def list_topic_resources(session: Session, topic_id: str) -> list[Resource]:
    """Resources linked to one topic, ordered by creation then id."""
    _topic_or_error(session, topic_id)
    statement = (
        select(Resource)
        .join(ResourceTopic, ResourceTopic.resource_id == Resource.id)
        .where(ResourceTopic.topic_id == topic_id)
        .order_by(ResourceTopic.created_at, Resource.id)
    )
    return list(session.scalars(statement))


def list_resources_for_topic(session: Session, topic_id: str) -> list[Resource]:
    """Alias of :func:`list_topic_resources` under the wording of the contract."""
    return list_topic_resources(session, topic_id)


def list_task_resources(session: Session, task_id: str) -> list[Resource]:
    """Resources attached to one task, ordered by creation then id."""
    _task_or_error(session, task_id)
    statement = (
        select(Resource)
        .join(TaskResource, TaskResource.resource_id == Resource.id)
        .where(TaskResource.task_id == task_id)
        .order_by(TaskResource.created_at, Resource.id)
    )
    return list(session.scalars(statement))


def list_resources_for_task(session: Session, task_id: str) -> list[Resource]:
    """Read resource links for one task; this grants no original-file access."""
    return list_task_resources(session, task_id)
