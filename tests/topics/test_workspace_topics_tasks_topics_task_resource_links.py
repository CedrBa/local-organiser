"""Task resource links contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from organiser.db.models import Event, Resource, ResourceTopic, TaskResource
from organiser.services.domain import (
    ConflictError,
    create_task,
    list_events_for_entity,
)
from organiser.services.topics import (
    create_topic,
    link_resource_topic,
    link_task_resource,
    unlink_task_resource,
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


# -- task <-> resource ---------------------------------------------------------


def test_link_task_resource_requires_task_context(session, context_a):
    resource = make_resource(session, context_a)
    task = create_task(session, title="Ohne Context")
    with pytest.raises(ConflictError):
        link_task_resource(session, task_id=task.id, resource_id=resource.id)


def test_link_task_resource_requires_same_context(session, context_a, context_b):
    resource = make_resource(session, context_b)
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    with pytest.raises(ConflictError):
        link_task_resource(session, task_id=task.id, resource_id=resource.id)


def test_link_task_resource_is_idempotent_and_audited(session, context_a):
    resource = make_resource(session, context_a)
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    assert session.query(TaskResource).filter_by(task_id=task.id).count() == 1
    events = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="task", entity_id=task.id)
    ]
    assert events.count("TASK_RESOURCE_LINKED") == 1
    unlink_task_resource(session, task_id=task.id, resource_id=resource.id)
    unlink_task_resource(session, task_id=task.id, resource_id=resource.id)
    assert session.query(TaskResource).filter_by(task_id=task.id).count() == 0
    events = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="task", entity_id=task.id)
    ]
    assert events.count("TASK_RESOURCE_UNLINKED") == 1


def test_task_resource_unique_conflict_keeps_caller_transaction(
    session, tmp_path, context_a, monkeypatch
):
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    resource = make_resource(session, context_a)
    original = link_task_resource(session, task_id=task.id, resource_id=resource.id)
    session.commit()
    session.expunge(original)

    caller_resource, caller_event = add_caller_work(session, context_a.id, flush=False)
    session.info["defer_domain_commit"] = True
    real_get = session.get
    missed = False

    def miss_link_once(model, identity, *args, **kwargs):
        nonlocal missed
        if model is TaskResource and not missed:
            missed = True
            return None  # Force the INSERT to encounter the database's composite PK.
        return real_get(model, identity, *args, **kwargs)

    monkeypatch.setattr(session, "get", miss_link_once)
    try:
        duplicate = link_task_resource(session, task_id=task.id, resource_id=resource.id)
        assert missed and (duplicate.task_id, duplicate.resource_id) == (task.id, resource.id)
        assert driver_transaction_is_open(session)
        assert_caller_work_intact(
            session, observer_factory(tmp_path), caller_resource, caller_event, topics=0
        )
        assert session.query(TaskResource).count() == 1
        assert [
            event.event_type
            for event in list_events_for_entity(session, entity_type="task", entity_id=task.id)
        ].count("TASK_RESOURCE_LINKED") == 1
        assert link_task_resource(session, task_id=task.id, resource_id=resource.id) is duplicate
    finally:
        session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Resource, caller_resource.id) is None
    assert session.get(TaskResource, (task.id, resource.id)) is not None


@pytest.mark.parametrize("kind", ["topic", "task"])
def test_link_does_not_classify_foreign_integrity_error_as_duplicate(session, context_a, kind):
    resource = make_resource(session, context_a)
    if kind == "topic":
        owner = create_topic(session, context_id=context_a.id, title="Bilanzierung")
        link, ids, model = link_resource_topic, {"topic_id": owner.id}, ResourceTopic
        event_type = "RESOURCE_TOPIC_LINKED"
    else:
        owner = create_task(session, title="Aufgabe", context_id=context_a.id)
        link, ids, model = link_task_resource, {"task_id": owner.id}, TaskResource
        event_type = "TASK_RESOURCE_LINKED"
    session.execute(
        text(
            f"CREATE TRIGGER fail_link_audit BEFORE INSERT ON events "
            f"WHEN NEW.event_type = '{event_type}' "
            "BEGIN SELECT RAISE(ABORT, 'foreign link audit failure'); END"
        )
    )
    session.commit()

    caller_resource, caller_event = add_caller_work(session, context_a.id, flush=False)
    session.info["defer_domain_commit"] = True
    try:
        with pytest.raises(IntegrityError, match="foreign link audit failure"):
            link(session, resource_id=resource.id, **ids)
        assert driver_transaction_is_open(session)
        assert session.get(Resource, caller_resource.id) is not None
        assert session.query(Event).filter_by(id=caller_event.id).count() == 1
        assert session.query(model).count() == 0
    finally:
        session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Resource, caller_resource.id) is None


def test_linking_a_resource_grants_no_read_access(session, context_a, knowledge_root):
    """Eine Verknuepfung ist eine Listenzeile, kein Lese- oder Scan-Recht."""
    before = sorted(item.name for item in knowledge_root.rglob("*"))
    resource = make_resource(session, context_a)
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    link_task_resource(
        session,
        task_id=create_task(session, title="X", context_id=context_a.id).id,
        resource_id=resource.id,
    )
    assert sorted(item.name for item in knowledge_root.rglob("*")) == before
    # Auch die Resource-Zeile selbst bekommt keinen Snapshot.
    assert session.query(Resource).filter_by(id=resource.id).one().current_content_hash is None
