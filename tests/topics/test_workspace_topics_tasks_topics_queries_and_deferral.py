"""Queries and deferral contracts for topics."""

from __future__ import annotations

import pytest
from datetime import UTC, datetime
from sqlalchemy import select
from conftest import make_settings
from organiser.db.models import Event, ResourceTopic, Task, TaskResource, Topic
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    create_task,
)
from organiser.services.topics import (
    create_topic,
    link_resource_topic,
    link_task_resource,
    list_topics,
    set_topic_status,
    unlink_resource_topic,
    unlink_task_resource,
    update_topic,
)
from support.workspace_topics_tasks_topics import (
    session as session,
    context_a as context_a,
    context_b as context_b,
    make_resource as make_resource,
    write_non_canonical_topic as write_non_canonical_topic,
    stored_topic_row as stored_topic_row,
    insert_rival_topic as insert_rival_topic,
    driver_transaction_is_open as driver_transaction_is_open,
    observer_factory as observer_factory,
    add_caller_work as add_caller_work,
    add_pending_colliding_topic as add_pending_colliding_topic,
    assert_caller_work_intact as assert_caller_work_intact,
    insert_duplicate_titles as insert_duplicate_titles,
    a_topic as a_topic,
    install_failing_audit_trigger as install_failing_audit_trigger,
)


# -- listing -------------------------------------------------------------------


def test_list_topics_is_ordered_by_title_key(session, context_a):
    for title in ("Zinsen", "Abschreibung", "Bilanzierung"):
        create_topic(session, context_id=context_a.id, title=title)
    assert [topic.title for topic in list_topics(session, context_a.id)] == [
        "Abschreibung",
        "Bilanzierung",
        "Zinsen",
    ]


def test_list_topics_filters_by_status(session, context_a):
    first = create_topic(session, context_id=context_a.id, title="A")
    create_topic(session, context_id=context_a.id, title="B")
    set_topic_status(session, topic_id=first.id, status="done")
    assert [topic.title for topic in list_topics(session, context_a.id, status="done")] == ["A"]


def test_list_topics_validates_context_and_status(session, context_a):
    with pytest.raises(NotFoundError):
        list_topics(session, "missing")
    with pytest.raises(DomainValidationError):
        list_topics(session, context_a.id, status="archived")


def test_topics_are_scoped_to_their_context(session, context_a, context_b):
    create_topic(session, context_id=context_a.id, title="Nur A")
    create_topic(session, context_id=context_b.id, title="Nur B")
    assert [topic.title for topic in list_topics(session, context_a.id)] == ["Nur A"]


# -- deferred commit -----------------------------------------------------------


def test_deferred_topic_create_rolls_back_with_the_caller(session, tmp_path, context_a):
    session.info["defer_domain_commit"] = True
    try:
        topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
        session.flush()
        other = create_session_factory(make_settings(tmp_path, "topics.sqlite3"))()
        try:
            assert other.get(Topic, topic.id) is None
        finally:
            other.close()
    finally:
        session.info.pop("defer_domain_commit")
    topic_id = topic.id
    session.rollback()
    assert session.get(Topic, topic_id) is None


def test_deferred_link_rolls_back_with_the_caller(session, tmp_path, context_a):
    resource = make_resource(session, context_a)
    session.commit()
    session.info["defer_domain_commit"] = True
    try:
        topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
        link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
        session.flush()
    finally:
        session.info.pop("defer_domain_commit")
    session.rollback()
    assert list_topics(session, context_a.id) == []
    assert session.query(ResourceTopic).count() == 0


def test_compatibility_uppercase_duplicate_conflicts_on_create_and_rename(session, context_a):
    create_topic(session, context_id=context_a.id, title="A")
    other = create_topic(session, context_id=context_a.id, title="B")
    with pytest.raises(ConflictError):
        create_topic(session, context_id=context_a.id, title="𝐀")
    with pytest.raises(ConflictError):
        update_topic(session, topic_id=other.id, title="𝐀")
    assert other.title == "B"


@pytest.mark.parametrize("kind", ["topic", "task"])
def test_link_mutations_invalidate_owner_versions_without_noop_churn(session, context_a, kind):
    resource = make_resource(session, context_a)
    if kind == "topic":
        owner = create_topic(session, context_id=context_a.id, title="Bilanzierung")
        link, unlink = link_resource_topic, unlink_resource_topic
        ids = {"topic_id": owner.id, "resource_id": resource.id}
    else:
        owner = create_task(session, title="Uebung", context_id=context_a.id)
        link, unlink = link_task_resource, unlink_task_resource
        ids = {"task_id": owner.id, "resource_id": resource.id}
    old = datetime(2000, 1, 1, tzinfo=UTC)
    owner.updated_at = resource.updated_at = old
    session.commit()
    link(session, **ids)
    version = owner.updated_at
    assert version != old and resource.updated_at == version
    link(session, **ids)
    assert owner.updated_at == version and resource.updated_at == version
    owner.updated_at = resource.updated_at = old
    session.commit()
    unlink(session, **ids)
    assert owner.updated_at != old and resource.updated_at == owner.updated_at
    version = owner.updated_at
    unlink(session, **ids)
    assert owner.updated_at == version


@pytest.mark.parametrize("kind", ["topic", "task"])
def test_unlink_rejects_cross_context_even_when_no_link_exists(session, context_a, context_b, kind):
    resource = make_resource(session, context_b)
    if kind == "topic":
        owner = create_topic(session, context_id=context_a.id, title="X")
        operation, ids = unlink_resource_topic, {"topic_id": owner.id}
    else:
        owner = create_task(session, title="X", context_id=context_a.id)
        operation, ids = unlink_task_resource, {"task_id": owner.id}
    with pytest.raises(ConflictError):
        operation(session, resource_id=resource.id, **ids)


def test_deferred_update_status_and_unlink_restore_all_rows_and_events(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Uebung", context_id=context_a.id)
    resource = make_resource(session, context_a)
    link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    topic_id, task_id, resource_id = topic.id, task.id, resource.id
    before_events = list(session.scalars(select(Event.id)))
    session.commit()
    session.info["defer_domain_commit"] = True
    update_topic(session, topic_id=topic_id, title="Neu")
    set_topic_status(session, topic_id=topic_id, status="done")
    unlink_resource_topic(session, topic_id=topic_id, resource_id=resource_id)
    unlink_task_resource(session, task_id=task_id, resource_id=resource_id)
    session.info.pop("defer_domain_commit")
    session.rollback()
    restored = session.get(Topic, topic_id)
    assert (restored.title, restored.status) == ("Bilanzierung", "not_started")
    assert session.get(Task, task_id).status == "open"
    assert session.get(ResourceTopic, (resource_id, topic_id)) is not None
    assert session.get(TaskResource, (task_id, resource_id)) is not None
    assert list(session.scalars(select(Event.id))) == before_events
