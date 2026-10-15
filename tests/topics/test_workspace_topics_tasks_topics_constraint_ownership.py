"""Constraint ownership contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from organiser.db.models import Event, Resource, Topic
from organiser.services.domain import (
    ConflictError,
    EventContext,
    record_event,
    list_events_for_entity,
)
from organiser.services.topics import (
    _title_uniqueness_boundary,
    create_topic,
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


# -- the flush that decides is a real one, through the real function -----------


def test_duplicate_create_is_a_real_integrity_error_not_only_a_precheck(
    session, tmp_path, context_a
):
    """Finding: the pre-check is blind to the caller's own pending rows.

    ``autoflush=False`` means the clash SELECT neither flushes nor sees the pending
    topic, so the refusal really comes from ``uq_topics_context_title_key`` at flush
    time. Proven by the exception cause, not assumed.
    """
    session.info["defer_domain_commit"] = True
    resource, event = add_caller_work(session, context_a.id, flush=True)
    pending = add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")
    assert driver_transaction_is_open(session)

    with pytest.raises(ConflictError) as failure:
        create_topic(session, context_id=context_a.id, title="Bilanzierung")

    assert isinstance(failure.value.__cause__, IntegrityError)
    assert_caller_work_intact(session, observer_factory(tmp_path), resource, event, topics=1)
    assert session.get(Topic, pending.id).title_key == "bilanzierung"


def test_duplicate_create_preserves_flushed_and_pending_caller_work(session, tmp_path, context_a):
    """Both caller flavours at once, plus no early commit and no stray audit."""
    session.info["defer_domain_commit"] = True
    resource, event = add_caller_work(session, context_a.id, flush=False)
    pending = add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")
    assert not driver_transaction_is_open(session)  # logical txn only, pending state

    with pytest.raises(ConflictError) as failure:
        create_topic(session, context_id=context_a.id, title="Bilanzierung")

    assert isinstance(failure.value.__cause__, IntegrityError)
    assert driver_transaction_is_open(session)
    assert_caller_work_intact(session, observer_factory(tmp_path), resource, event, topics=1)
    # The refused operation must leave no audit behind -- not inside, not outside.
    assert not [e for e in session.query(Event).filter(Event.event_type == "TOPIC_CREATED").all()]
    with observer_factory(tmp_path)() as other:
        assert other.query(Event).filter(Event.event_type == "TOPIC_CREATED").count() == 0

    # The caller still owns the outcome: its commit keeps its work and discards ours.
    session.info.pop("defer_domain_commit")
    session.commit()
    assert session.get(Resource, resource.id) is not None
    assert session.query(Topic).count() == 1
    assert session.get(Topic, pending.id).id == pending.id


def test_caller_rollback_after_a_duplicate_create_discards_everything(session, tmp_path, context_a):
    """The other half of the contract: we commit nothing, so the caller can undo all."""
    session.info["defer_domain_commit"] = True
    resource, event = add_caller_work(session, context_a.id, flush=False)
    pending = add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")

    with pytest.raises(ConflictError):
        create_topic(session, context_id=context_a.id, title="Bilanzierung")

    session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Resource, resource.id) is None
    assert session.get(Topic, pending.id) is None
    assert session.query(Event).filter(Event.event_type == "CALLER_EVENT").count() == 0
    with observer_factory(tmp_path)() as other:
        assert other.query(Resource).count() == 0
        assert other.query(Topic).count() == 0


@pytest.mark.parametrize("flush", [True, False], ids=["flushed_caller", "pending_caller"])
def test_rename_conflict_does_not_roll_back_the_caller_transaction(
    session, tmp_path, context_a, flush
):
    """Finding: a rename collision used to travel through ``_commit``.

    ``_commit`` rolls the whole session back on failure, so under
    ``defer_domain_commit`` a duplicate rename silently discarded the entire
    proposal: measured, the caller's flushed ``Resource`` was gone and the driver
    transaction was closed afterwards. The rename is now contained, and whether the
    clash is decided by the pre-check or by the flush, the caller's state survives.
    """
    renamed = create_topic(session, context_id=context_a.id, title="Alpha")
    create_topic(session, context_id=context_a.id, title="Beta")
    session.commit()
    session.info["defer_domain_commit"] = True
    resource, event = add_caller_work(session, context_a.id, flush=flush)
    pending = add_pending_colliding_topic(session, context_a.id, "Gamma", "gamma")

    with pytest.raises(ConflictError):
        update_topic(session, topic_id=renamed.id, title="Gamma")

    # The caller's transaction survives in both flavours, and nothing was committed.
    assert driver_transaction_is_open(session)
    assert_caller_work_intact(session, observer_factory(tmp_path), resource, event, topics=3)
    restored = session.get(Topic, renamed.id)
    assert (restored.title, restored.title_key) == ("Alpha", "alpha")
    assert not [e for e in session.query(Event).filter(Event.event_type == "TOPIC_UPDATED").all()]
    with observer_factory(tmp_path)() as other:
        assert other.get(Topic, renamed.id).title == "Alpha"
        assert other.query(Event).filter(Event.event_type == "TOPIC_UPDATED").count() == 0

    # The caller still owns the outcome.
    session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Resource, resource.id) is None
    assert session.get(Topic, pending.id) is None


def test_operation_audit_is_rolled_back_with_the_refused_operation(session, tmp_path, context_a):
    """An audit event added inside the boundary dies with the refused operation.

    This is the property ``update_topic`` relies on: the ``TOPIC_UPDATED`` event is
    written inside the boundary, so a refused rename leaves no dangling audit entry.
    The constraint is provoked for real, not simulated.
    """
    session.info["defer_domain_commit"] = True
    marker = make_resource(session, context_a, title="Caller-Arbeit")
    session.flush()

    with pytest.raises(IntegrityError):
        with _title_uniqueness_boundary(session):
            record_event(
                session,
                event_type="OPERATION_AUDIT",
                entity_type="context",
                entity_id=context_a.id,
                payload={},
                context=None,
            )
            insert_duplicate_titles(session, context_a.id, "Bilanzierung", "bilanzierung")

    assert session.query(Event).filter(Event.event_type == "OPERATION_AUDIT").count() == 0
    with observer_factory(tmp_path)() as other:
        assert other.query(Event).filter(Event.event_type == "OPERATION_AUDIT").count() == 0
    assert session.get(Resource, marker.id) is not None
    assert session.query(Topic).count() == 0


def test_a_successful_rename_still_commits_and_audits(session, context_a):
    """The boundary must not swallow the happy path."""
    topic = create_topic(session, context_id=context_a.id, title="Alpha")
    renamed = update_topic(
        session,
        topic_id=topic.id,
        title="Gamma",
        context=EventContext(source_type="context_action_proposal", source_id="p-1"),
    )
    assert (renamed.title, renamed.title_key) == ("Gamma", "gamma")
    audit = list_events_for_entity(session, entity_type="topic", entity_id=topic.id)[-1]
    assert audit.event_type == "TOPIC_UPDATED" and audit.source_id == "p-1"
