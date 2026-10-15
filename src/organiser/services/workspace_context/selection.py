"""Select one context and its scoped entities with bounded database queries."""

from __future__ import annotations

from typing import Iterable, Sequence
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    ContextSource,
    Event,
    Resource,
    ResourceTopic,
    Task,
    TaskResource,
    Topic,
)
from organiser.services.domain import NotFoundError
from .constants import OPEN_TASK_STATUSES
from .events import _count, _count_events, _recent_events
from .policy import WorkspacePolicy, WorkspaceSelectionError
from .snapshots import _classify_resources, _load_snapshots
from .state import _Prepared


# ---------------------------------------------------------------------------
# Loading and focus validation
# ---------------------------------------------------------------------------


def _load_context(session: Session, context_id: str) -> Context:
    if not isinstance(context_id, str) or not context_id.strip():
        raise WorkspaceSelectionError("context_id darf nicht leer sein")
    context = session.get(Context, context_id)
    if context is None:
        raise NotFoundError("Context nicht gefunden")
    return context


def _validate_focus(
    session: Session,
    context: Context,
    model: type,
    field: str,
    values: Sequence[str],
    policy: WorkspacePolicy,
) -> tuple[str, ...]:
    """Refuse any explicitly requested ID that is not part of this context.

    A requested ID is a hard filter, not a hint. Accepting a foreign ID would either
    leak another context's rows into this packet or silently resolve a similar title
    somewhere else; both are the fuzzy cross-context roaming the architecture
    forbids.
    """
    if not values:
        return ()
    if isinstance(values, str):
        raise WorkspaceSelectionError(f"{field} muss eine Liste von IDs sein")
    unique: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise WorkspaceSelectionError(f"{field} enthaelt eine ungueltige ID")
        if value not in unique:
            unique.append(value)
    if len(unique) > policy.max_focus_ids:
        raise WorkspaceSelectionError(
            f"{field} darf hoechstens {policy.max_focus_ids} IDs enthalten"
        )
    rows = session.execute(select(model.id, model.context_id).where(model.id.in_(unique))).all()
    found = {row[0]: row[1] for row in rows}
    for identifier in unique:
        if identifier not in found:
            raise WorkspaceSelectionError(
                f"{field} enthaelt die unbekannte ID {identifier}; es wurde nichts geraten"
            )
        if found[identifier] != context.id:
            raise WorkspaceSelectionError(
                f"{field} enthaelt die ID {identifier}, die nicht zum angefragten Context gehoert; "
                "es wurde nichts geraten"
            )
    return tuple(unique)


def _prepare(
    session: Session,
    context: Context,
    policy: WorkspacePolicy,
    wanted_topics: tuple[str, ...],
    wanted_resources: tuple[str, ...],
) -> _Prepared:
    """Load the bounded selection and counts for exactly one context; do not mutate it."""
    prepared = _Prepared(
        context=context,
        event_max_id=int(session.scalar(select(func.max(Event.id))) or 0),
        counts={
            "tasks": _count(session, Task, Task.context_id == context.id),
            "open_tasks": _count(
                session, Task, Task.context_id == context.id, Task.status.in_(OPEN_TASK_STATUSES)
            ),
            "topics": _count(session, Topic, Topic.context_id == context.id),
            "resources": _count(session, Resource, Resource.context_id == context.id),
            "sources": _count(session, ContextSource, ContextSource.context_id == context.id),
        },
    )

    # Topics: status, then title_key, then id. An explicitly focused topic is read
    # in a separate bounded query, so focus is never silently lost because the
    # canonical ordering pushed the row past the LIMIT.
    focused_topics: list[Topic] = []
    if wanted_topics:
        focused_topics = list(
            session.scalars(
                select(Topic)
                .where(Topic.context_id == context.id, Topic.id.in_(wanted_topics))
                .order_by(Topic.status, Topic.title_key, Topic.id)
                .limit(policy.max_focus_ids)
            )
        )
    other_topics = list(
        session.scalars(
            select(Topic)
            .where(
                Topic.context_id == context.id,
                Topic.id.not_in([topic.id for topic in focused_topics])
                if focused_topics
                else Topic.id.is_not(None),
            )
            .order_by(Topic.status, Topic.title_key, Topic.id)
            .limit(policy.max_topics)
        )
    )
    prepared.topics = [*focused_topics, *other_topics]

    # Open tasks: due_at NULL last, then due_at, created_at, id.
    prepared.tasks = list(
        session.scalars(
            select(Task)
            .where(Task.context_id == context.id, Task.status.in_(OPEN_TASK_STATUSES))
            .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at, Task.id)
            .limit(policy.max_tasks)
        )
    )

    prepared.sources = list(
        session.scalars(
            select(ContextSource)
            .where(ContextSource.context_id == context.id)
            .order_by(ContextSource.created_at, ContextSource.id)
            .limit(policy.max_sources)
        )
    )
    prepared.source_by_id = {source.id: source for source in prepared.sources}

    prepared.resources = _select_resources(
        session,
        context,
        policy,
        wanted_topics,
        wanted_resources,
        [topic.id for topic in prepared.topics],
    )
    resource_ids = [resource.id for resource in prepared.resources]
    task_ids = [task.id for task in prepared.tasks]
    topic_ids = [topic.id for topic in prepared.topics]

    _load_referenced_sources(session, prepared, context)
    _load_snapshots(session, prepared, resource_ids, policy)
    _load_links(session, prepared, resource_ids, task_ids, topic_ids, policy)
    prepared.events = _recent_events(session, context, resource_ids, task_ids, topic_ids, policy)
    prepared.counts["events"] = _count_events(session, context, resource_ids, task_ids, topic_ids)
    # Counted here, once, from the rows that were actually selected -- not summed over
    # the reduction passes, which would multiply the number by the number of passes.
    prepared.counts["event_payloads"] = sum(1 for row in prepared.events if not row.payload_loaded)
    _classify_resources(prepared, policy)
    prepared.resource_candidate_total = len(prepared.candidates)
    return prepared


def _select_resources(
    session: Session,
    context: Context,
    policy: WorkspacePolicy,
    wanted_topics: tuple[str, ...],
    wanted_resources: tuple[str, ...],
    selected_topic_ids: Sequence[str],
) -> list[Resource]:
    """Rank resources by explicit focus, then topic/task relations, then recency.

    Only direct assignments of this context are followed: no directory walk, no
    source enumeration, no child contexts.
    """
    limit = policy.max_resources
    selected: list[Resource] = []
    seen: set[str] = set()

    def add(resources: Iterable[Resource]) -> None:
        for resource in resources:
            if len(selected) >= limit:
                return
            if resource.id in seen:
                # Already collected by an earlier, higher-ranked branch. Skipping --
                # not returning -- is what lets the remaining rows of this branch
                # through.
                continue
            seen.add(resource.id)
            selected.append(resource)

    if wanted_resources:
        add(
            session.scalars(
                select(Resource)
                .where(Resource.context_id == context.id, Resource.id.in_(wanted_resources))
                .order_by(Resource.last_seen_at.desc(), Resource.id)
                .limit(limit)
            )
        )
    if wanted_topics:
        add(
            session.scalars(
                select(Resource)
                .join(ResourceTopic, ResourceTopic.resource_id == Resource.id)
                .where(Resource.context_id == context.id, ResourceTopic.topic_id.in_(wanted_topics))
                .order_by(Resource.last_seen_at.desc(), Resource.id)
                .limit(limit)
            )
        )
        # A focused topic also surfaces the resources of its open tasks.
        add(
            session.scalars(
                select(Resource)
                .join(TaskResource, TaskResource.resource_id == Resource.id)
                .join(Task, Task.id == TaskResource.task_id)
                .where(
                    Resource.context_id == context.id,
                    Task.context_id == context.id,
                    Task.topic_id.in_(wanted_topics),
                )
                .order_by(Resource.last_seen_at.desc(), Resource.id)
                .limit(limit)
            )
        )
    if selected_topic_ids:
        # Even without an explicit focus, a resource that is actually assigned to a
        # selected topic outranks one that is merely recent.
        add(
            session.scalars(
                select(Resource)
                .join(ResourceTopic, ResourceTopic.resource_id == Resource.id)
                .where(
                    Resource.context_id == context.id,
                    ResourceTopic.topic_id.in_(selected_topic_ids),
                )
                .order_by(Resource.last_seen_at.desc(), Resource.id)
                .limit(limit)
            )
        )
    if (
        session.scalar(
            select(Task.id)
            .where(Task.context_id == context.id, Task.status.in_(OPEN_TASK_STATUSES))
            .limit(1)
        )
        is not None
    ):
        add(
            session.scalars(
                select(Resource)
                .join(TaskResource, TaskResource.resource_id == Resource.id)
                .join(Task, Task.id == TaskResource.task_id)
                .where(
                    Resource.context_id == context.id,
                    Task.context_id == context.id,
                    Task.status.in_(OPEN_TASK_STATUSES),
                )
                .order_by(Resource.last_seen_at.desc(), Resource.id)
                .limit(limit)
            )
        )
    add(
        session.scalars(
            select(Resource)
            .where(Resource.context_id == context.id)
            .order_by(Resource.last_seen_at.desc(), Resource.id)
            .limit(limit)
        )
    )
    return selected[:limit]


def _load_referenced_sources(session: Session, prepared: _Prepared, context: Context) -> None:
    """Resolve exactly the sources the selected resources point at.

    The staleness check in :func:`_classify_resources` asks whether a resource's
    source is still active. Reading that from the *display* list of sources would
    silently degrade the answer to "is the source one of the first ten by creation
    time", which would let an archived source keep its document presented as current.
    The two sets are therefore loaded separately, and the lookup set is bounded by
    the number of selected resources.
    """
    referenced = {resource.source_id for resource in prepared.resources if resource.source_id}
    if not referenced:
        return
    rows = session.scalars(
        select(ContextSource)
        .where(ContextSource.id.in_(referenced))
        .order_by(ContextSource.created_at, ContextSource.id)
        .limit(len(referenced))
    )
    for source in rows:
        # A source of a *different* context is treated as inactive even if its own
        # status says active: the schema does not forbid it, and a resource must not
        # gain read access to another context's source through this read model.
        if source.context_id != context.id:
            prepared.unusable_sources[source.id] = source
            continue
        prepared.referenced_sources[source.id] = source


def _load_links(
    session: Session,
    prepared: _Prepared,
    resource_ids: Sequence[str],
    task_ids: Sequence[str],
    topic_ids: Sequence[str],
    policy: WorkspacePolicy,
) -> None:
    """Load link tables in bounded queries and cap them per owner.

    A link pointing outside the current selection is counted as omitted, never
    followed: following it would widen the packet past the resource budget.
    """
    per_item = policy.max_ids_per_item

    def bucket(links: dict[str, list[str]], key: str, value: str) -> None:
        entries = links.setdefault(key, [])
        if len(entries) < per_item:
            entries.append(value)

    if topic_ids:
        rows = session.execute(
            select(ResourceTopic.topic_id, ResourceTopic.resource_id)
            .where(ResourceTopic.topic_id.in_(topic_ids))
            .order_by(ResourceTopic.topic_id, ResourceTopic.resource_id)
            .limit(len(topic_ids) * (per_item + 1))
        ).all()
        prepared.counts["topic_resource_links"] = len(rows)
        for topic_id, resource_id in rows:
            if resource_id not in resource_ids:
                continue
            bucket(prepared.topic_resource_links, topic_id, resource_id)
            prepared.topic_resource_counts[topic_id] = (
                prepared.topic_resource_counts.get(topic_id, 0) + 1
            )
    if resource_ids:
        for resource_id, topic_id in session.execute(
            select(ResourceTopic.resource_id, ResourceTopic.topic_id)
            .where(ResourceTopic.resource_id.in_(resource_ids))
            .order_by(ResourceTopic.resource_id, ResourceTopic.topic_id)
            .limit(len(resource_ids) * (per_item + 1))
        ).all():
            if topic_id not in topic_ids:
                continue
            bucket(prepared.resource_topic_links, resource_id, topic_id)
            prepared.resource_topic_counts[resource_id] = (
                prepared.resource_topic_counts.get(resource_id, 0) + 1
            )
    if task_ids:
        rows = session.execute(
            select(TaskResource.task_id, TaskResource.resource_id)
            .where(TaskResource.task_id.in_(task_ids))
            .order_by(TaskResource.task_id, TaskResource.resource_id)
            .limit(len(task_ids) * (per_item + 1))
        ).all()
        prepared.counts["task_resource_links"] = len(rows)
        for task_id, resource_id in rows:
            if resource_id not in resource_ids:
                continue
            bucket(prepared.task_resource_links, task_id, resource_id)
            prepared.task_resource_counts[task_id] = (
                prepared.task_resource_counts.get(task_id, 0) + 1
            )
    if resource_ids:
        for resource_id, task_id in session.execute(
            select(TaskResource.resource_id, TaskResource.task_id)
            .where(TaskResource.resource_id.in_(resource_ids))
            .order_by(TaskResource.resource_id, TaskResource.task_id)
            .limit(len(resource_ids) * (per_item + 1))
        ).all():
            if task_id not in task_ids:
                continue
            bucket(prepared.resource_task_links, resource_id, task_id)
            prepared.resource_task_counts[resource_id] = (
                prepared.resource_task_counts.get(resource_id, 0) + 1
            )
