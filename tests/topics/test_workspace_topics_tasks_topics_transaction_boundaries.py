"""Transaction boundaries contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from organiser.db.models import Context, Resource, Topic
from organiser.services.domain import (
    list_events_for_entity,
)
from organiser.services.topics import (
    _flush_caller_state,
    _title_uniqueness_boundary,
    _real_transaction_is_open,
    create_topic,
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


# -- the boundary itself: both branches, measured -------------------------------


def test_the_savepoint_decision_follows_the_driver_and_not_the_logical_state(session, context_a):
    """The crux, measured: a logical transaction is not a SQLite transaction."""
    assert not _real_transaction_is_open(session)
    session.scalar(select(Context.id).where(Context.id == context_a.id))
    assert session.in_transaction()
    assert not driver_transaction_is_open(session)
    assert not _real_transaction_is_open(session)

    make_resource(session, context_a)
    assert driver_transaction_is_open(session)
    assert _real_transaction_is_open(session)


def test_pending_caller_state_moves_the_boundary_off_the_full_rollback_branch(session, context_a):
    """Finding: the check ran before the caller's pending flush.

    With pending rows and the driver still in autocommit the old order took the
    full-rollback branch and destroyed them. Now the pending state is flushed first,
    a real transaction exists, and the savepoint branch is used.
    """
    resource, event = add_caller_work(session, context_a.id, flush=False)
    pending = add_pending_colliding_topic(session, context_a.id, "X", "x")
    assert not driver_transaction_is_open(session)

    with _title_uniqueness_boundary(session):
        assert _real_transaction_is_open(session)

    # Flushed, still pending-free, still uncommitted, still ours to decide on.
    assert not session.new
    assert session.get(Resource, resource.id) is not None
    assert session.get(Topic, pending.id) is not None


def test_a_real_violation_inside_a_caller_transaction_leaves_the_caller_in_control(
    session, tmp_path, context_a
):
    """The E situation: a proposal transaction with its own flushed rows."""
    session.info["defer_domain_commit"] = True
    marker = make_resource(session, context_a, title="Caller-Arbeit")
    session.flush()
    assert _real_transaction_is_open(session)

    with pytest.raises(IntegrityError):
        with _title_uniqueness_boundary(session):
            insert_duplicate_titles(session, context_a.id, "Bilanzierung", "bilanzierung")

    assert _real_transaction_is_open(session)
    assert session.get(Resource, marker.id) is not None
    assert session.query(Topic).count() == 0

    session.info.pop("defer_domain_commit")
    session.commit()
    assert session.get(Resource, marker.id) is not None
    assert session.query(Topic).count() == 0


def test_a_real_violation_without_caller_state_keeps_the_rollback_convention(
    session, tmp_path, context_a
):
    """No caller state means nothing to protect, so the old convention applies.

    A savepoint must not be used here -- SQLite would commit on ``RELEASE
    SAVEPOINT``. What must hold either way is that the refused rows never become
    visible to anybody else.
    """
    assert not _real_transaction_is_open(session)

    with pytest.raises(IntegrityError):
        with _title_uniqueness_boundary(session):
            insert_duplicate_titles(session, context_a.id, "Bilanzierung", "bilanzierung")

    with observer_factory(tmp_path)() as other:
        assert other.query(Topic).count() == 0
    assert session.query(Topic).count() == 0
    assert create_topic(session, context_id=context_a.id, title="Bilanzierung").id


def test_preflush_failures_stay_caller_owned(session, context_a):
    """A caller collision is not ours to label, clean up or convert."""
    add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")
    add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")

    with pytest.raises(IntegrityError):
        _flush_caller_state(session)

    # We did not roll back and did not wrap it: the caller decides.
    assert session.new or session.in_transaction()


def test_a_conflict_free_create_is_unchanged_by_the_title_uniqueness_boundary(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    assert topic.knowledge_key == f"topic-{topic.id}"
    assert session.get(Topic, topic.id) is not None
    assert (
        list_events_for_entity(session, entity_type="topic", entity_id=topic.id)[-1].event_type
        == "TOPIC_CREATED"
    )
