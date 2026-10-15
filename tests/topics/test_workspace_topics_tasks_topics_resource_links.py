"""Resource links contracts for topics."""

from __future__ import annotations

import pytest
from organiser.db.models import Resource, ResourceTopic
from organiser.services.domain import (
    ConflictError,
    NotFoundError,
    list_events_for_entity,
)
from organiser.services.topics import (
    create_topic,
    link_resource_topic,
    list_topic_resources,
    unlink_resource_topic,
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


# -- resource <-> topic --------------------------------------------------------


def test_link_resource_topic_requires_same_context(session, context_a, context_b):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    foreign = make_resource(session, context_b)
    with pytest.raises(ConflictError):
        link_resource_topic(session, topic_id=topic.id, resource_id=foreign.id)


def test_link_resource_topic_is_idempotent(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    resource = make_resource(session, context_a)
    first = link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    second = link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    assert (first.resource_id, first.topic_id) == (second.resource_id, second.topic_id)
    assert session.query(ResourceTopic).filter_by(topic_id=topic.id).count() == 1


def test_resource_topic_unique_conflict_keeps_caller_transaction(
    session, tmp_path, context_a, monkeypatch
):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    resource = make_resource(session, context_a)
    original = link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    session.commit()
    session.expunge(original)

    caller_resource, caller_event = add_caller_work(session, context_a.id, flush=False)
    session.info["defer_domain_commit"] = True
    real_get = session.get
    missed = False

    def miss_link_once(model, identity, *args, **kwargs):
        nonlocal missed
        if model is ResourceTopic and not missed:
            missed = True
            return None  # Simulate a stale pre-check; the INSERT must hit the real PK.
        return real_get(model, identity, *args, **kwargs)

    monkeypatch.setattr(session, "get", miss_link_once)
    try:
        duplicate = link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
        assert missed and (duplicate.resource_id, duplicate.topic_id) == (resource.id, topic.id)
        assert driver_transaction_is_open(session)
        assert_caller_work_intact(
            session, observer_factory(tmp_path), caller_resource, caller_event, topics=1
        )
        assert session.query(ResourceTopic).count() == 1
        assert [
            event.event_type
            for event in list_events_for_entity(session, entity_type="topic", entity_id=topic.id)
        ].count("RESOURCE_TOPIC_LINKED") == 1
        assert link_resource_topic(session, topic_id=topic.id, resource_id=resource.id) is duplicate
    finally:
        session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Resource, caller_resource.id) is None
    assert session.get(ResourceTopic, (resource.id, topic.id)) is not None


def test_unlink_resource_topic_is_idempotent(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    resource = make_resource(session, context_a)
    link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    unlink_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    unlink_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    assert session.query(ResourceTopic).filter_by(topic_id=topic.id).count() == 0


def test_listing_topic_resources(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    first = make_resource(session, context_a, "A")
    second = make_resource(session, context_a, "B")
    link_resource_topic(session, topic_id=topic.id, resource_id=second.id)
    link_resource_topic(session, topic_id=topic.id, resource_id=first.id)
    assert [item.id for item in list_topic_resources(session, topic.id)] == [second.id, first.id]


def test_link_unknown_resource_or_topic_raises(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    with pytest.raises(NotFoundError):
        link_resource_topic(session, topic_id=topic.id, resource_id="missing")
    with pytest.raises(NotFoundError):
        link_resource_topic(session, topic_id="missing", resource_id="missing")
