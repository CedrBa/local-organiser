"""Context-consistent resource links with idempotency and caller transaction ownership."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from organiser.db.models import (
    ResourceTopic,
    TaskResource,
    utc_now,
)
from organiser.services.domain import (
    ConflictError,
    EventContext,
    _commit,
    record_event,
)
from .bindings import _resource_or_error, _task_or_error, _topic_or_error
from .constraints import _constraint_boundary, _link_key_uniqueness_violated


TOPIC_CREATED_EVENT = "TOPIC_CREATED"


TOPIC_UPDATED_EVENT = "TOPIC_UPDATED"


TOPIC_STATUS_CHANGED_EVENT = "TOPIC_STATUS_CHANGED"


RESOURCE_TOPIC_LINKED_EVENT = "RESOURCE_TOPIC_LINKED"


RESOURCE_TOPIC_UNLINKED_EVENT = "RESOURCE_TOPIC_UNLINKED"


TASK_RESOURCE_LINKED_EVENT = "TASK_RESOURCE_LINKED"


TASK_RESOURCE_UNLINKED_EVENT = "TASK_RESOURCE_UNLINKED"


DEFAULT_TOPIC_STATUS = "not_started"


def link_resource_topic(
    session: Session, *, topic_id: str, resource_id: str, context: EventContext | None = None
) -> ResourceTopic:
    """Associate a resource with a topic. Idempotent.

    Both entities must already exist and must belong to the *same* context. This
    creates a database row and nothing else: no file is read, no snapshot is
    taken, and the resource gains no read permission for its source. A registry
    scan stays an explicit act of the ingestion path.
    """
    topic = _topic_or_error(session, topic_id)
    resource = _resource_or_error(session, resource_id)
    if resource.context_id != topic.context_id:
        raise ConflictError("Resource und Topic muessen demselben Context angehoeren")
    existing = session.get(ResourceTopic, {"resource_id": resource.id, "topic_id": topic.id})
    if existing is not None:
        return existing
    try:
        with _constraint_boundary(session):
            link = ResourceTopic(resource_id=resource.id, topic_id=topic.id)
            session.add(link)
            topic.updated_at = resource.updated_at = utc_now()
            record_event(
                session,
                event_type=RESOURCE_TOPIC_LINKED_EVENT,
                entity_type="topic",
                entity_id=topic.id,
                payload={
                    "linked": {
                        "resource_id": resource.id,
                        "topic_id": topic.id,
                        "context_id": topic.context_id,
                    }
                },
                context=context,
            )
            session.flush()
    except IntegrityError as error:
        if not _link_key_uniqueness_violated(error, "resource_topics", ("resource_id", "topic_id")):
            raise
        existing = session.get(ResourceTopic, {"resource_id": resource.id, "topic_id": topic.id})
        if existing is None:
            raise
        return existing
    _commit(session)
    return link


def unlink_resource_topic(
    session: Session, *, topic_id: str, resource_id: str, context: EventContext | None = None
) -> None:
    """Remove a resource/topic association. Idempotent for valid same-context pairs.

    Removing an association that does not exist is a success, not an error: the
    caller's intent -- "these two are not linked" -- already holds. Only a real
    removal produces an event.
    """
    topic = _topic_or_error(session, topic_id)
    resource = _resource_or_error(session, resource_id)
    if resource.context_id != topic.context_id:
        raise ConflictError("Resource und Topic muessen demselben Context angehoeren")
    existing = session.get(ResourceTopic, {"resource_id": resource.id, "topic_id": topic.id})
    if existing is None:
        return
    session.delete(existing)
    topic.updated_at = resource.updated_at = utc_now()
    record_event(
        session,
        event_type=RESOURCE_TOPIC_UNLINKED_EVENT,
        entity_type="topic",
        entity_id=topic.id,
        payload={
            "unlinked": {
                "resource_id": resource.id,
                "topic_id": topic.id,
                "context_id": topic.context_id,
            }
        },
        context=context,
    )
    _commit(session)


def link_task_resource(
    session: Session, *, task_id: str, resource_id: str, context: EventContext | None = None
) -> TaskResource:
    """Attach a resource as evidence for a task. Idempotent.

    A task may only reference a resource of its own context, so the task needs a
    context at all. A task without a context can reference no resource: that is
    refused here rather than stored as a cross-context reference.
    """
    task = _task_or_error(session, task_id)
    resource = _resource_or_error(session, resource_id)
    if task.context_id is None:
        raise ConflictError("Ein Task ohne Context kann keine Resource referenzieren")
    if resource.context_id != task.context_id:
        raise ConflictError("Resource und Task muessen demselben Context angehoeren")
    existing = session.get(TaskResource, {"task_id": task.id, "resource_id": resource.id})
    if existing is not None:
        return existing
    try:
        with _constraint_boundary(session):
            link = TaskResource(task_id=task.id, resource_id=resource.id)
            session.add(link)
            task.updated_at = resource.updated_at = utc_now()
            record_event(
                session,
                event_type=TASK_RESOURCE_LINKED_EVENT,
                entity_type="task",
                entity_id=task.id,
                payload={
                    "linked": {
                        "task_id": task.id,
                        "resource_id": resource.id,
                        "context_id": task.context_id,
                    }
                },
                context=context,
            )
            session.flush()
    except IntegrityError as error:
        if not _link_key_uniqueness_violated(error, "task_resources", ("task_id", "resource_id")):
            raise
        existing = session.get(TaskResource, {"task_id": task.id, "resource_id": resource.id})
        if existing is None:
            raise
        return existing
    _commit(session)
    return link


def unlink_task_resource(
    session: Session, *, task_id: str, resource_id: str, context: EventContext | None = None
) -> None:
    """Detach a resource from a task. Idempotent for valid same-context pairs."""
    task = _task_or_error(session, task_id)
    resource = _resource_or_error(session, resource_id)
    if task.context_id is None or resource.context_id != task.context_id:
        raise ConflictError("Resource und Task muessen demselben Context angehoeren")
    existing = session.get(TaskResource, {"task_id": task.id, "resource_id": resource.id})
    if existing is None:
        return
    session.delete(existing)
    task.updated_at = resource.updated_at = utc_now()
    record_event(
        session,
        event_type=TASK_RESOURCE_UNLINKED_EVENT,
        entity_type="task",
        entity_id=task.id,
        payload={"unlinked": {"task_id": task.id, "resource_id": resource.id}},
        context=context,
    )
    _commit(session)
