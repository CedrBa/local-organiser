"""Foreign constraints contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError, PendingRollbackError
from organiser.db.models import Event, Resource, Topic
from organiser.services.domain import (
    ConflictError,
)
from organiser.services.topics import (
    _title_key_uniqueness_violated,
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


def test_a_foreign_constraint_violation_is_not_renamed_into_a_duplicate_title(session, context_a):
    """Finding: every ``IntegrityError`` used to become "this title exists".

    The boundary's rollback is fine, but the *classification* was not: a
    ``CHECK(knowledge_key = 'topic-' || id)`` violation raised inside it was reported
    as a duplicate title, which sends the caller off renaming something that is not
    the problem. Only ``uq_topics_context_title_key`` may be called that.
    """
    broken = a_topic(context_a.id, title="Kaputt", title_key="kaputt", knowledge_key="topic-falsch")

    with pytest.raises(IntegrityError) as failure:
        with _title_uniqueness_boundary(session):
            session.add(broken)
            session.flush()

    assert not _title_key_uniqueness_violated(failure.value)
    assert "topics.title_key" not in str(failure.value.orig)


def test_a_check_violation_is_told_apart_from_the_title_key_violation(session, context_a):
    """The predicate is measured against real driver messages, not guessed ones.

    SQLite reports what it enforces, not the constraint name:
    ``CHECK constraint failed: ck_topics_knowledge_key`` for a hand-written key
    versus ``UNIQUE constraint failed: topics.context_id, topics.title_key`` for the
    duplicate this boundary is about. The predicate must accept the second and
    refuse the first.

    The sibling constraint ``uq_topics_context_knowledge_key`` cannot be provoked at
    all, and deliberately so: ``ck_topics_knowledge_key`` forces
    ``knowledge_key = 'topic-' || id``, so two rows sharing a knowledge key would
    also share a primary key. It is named here only to record that it was measured
    as unreachable instead of being assumed away.
    """
    with pytest.raises(IntegrityError) as check:
        with _title_uniqueness_boundary(session):
            session.add(
                a_topic(
                    context_a.id, title="Kaputt", title_key="kaputt", knowledge_key="topic-falsch"
                )
            )
            session.flush()

    with pytest.raises(IntegrityError) as wanted:
        with _title_uniqueness_boundary(session):
            insert_duplicate_titles(session, context_a.id, "Bilanzierung", "bilanzierung")

    assert not _title_key_uniqueness_violated(check.value)
    assert "ck_topics_knowledge_key" in str(check.value.orig)
    assert _title_key_uniqueness_violated(wanted.value)
    assert "topics.title_key" in str(wanted.value.orig)


def test_a_rename_over_a_defect_in_the_callers_staged_rows_is_not_a_conflict(session, context_a):
    """End-to-end: a foreign violation leaves ``update_topic`` as an ``IntegrityError``.

    A ``ConflictError`` here would promise the caller "pick another title", and
    picking another title would fail identically.
    """
    topic = create_topic(session, context_id=context_a.id, title="Alpha")
    session.add(
        a_topic(context_a.id, title="Kaputt", title_key="kaputt", knowledge_key="topic-falsch")
    )

    with pytest.raises(IntegrityError) as failure:
        update_topic(session, topic_id=topic.id, title="Beta")

    assert not isinstance(failure.value, ConflictError)


def test_a_caller_collision_is_not_reported_as_a_duplicate_title(session, context_a):
    """Caller-owned, on the create path too.

    ``create_topic`` flushes the caller's staged rows *outside* the boundary and
    outside the ``try``. The caller's own two rows collide on the very same
    ``uq_topics_context_title_key``, yet the answer must not be this operation's
    "duplicate title": the caller asked for one topic, and it is their two staged
    rows that clash.

    Measured for the assertions below: a failed flush makes SQLAlchemy drop the
    session's objects and put it into *pending rollback* -- further database access
    raises ``PendingRollbackError``. That is SQLAlchemy's own behaviour for every
    flush failure in this project, not something C adds. What C must not do is
    decide anything on the caller's behalf: the error is not converted, no rollback
    is issued, and the caller keeps the last word.
    """
    session.info["defer_domain_commit"] = True
    add_caller_work(session, context_a.id, flush=True)
    add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")
    add_pending_colliding_topic(session, context_a.id, "Bilanzierung", "bilanzierung")

    with pytest.raises(IntegrityError) as failure:
        create_topic(session, context_id=context_a.id, title="Ganz anderes Thema")

    assert not isinstance(failure.value, ConflictError)
    # Nothing was decided for the caller: no rollback, and the session sits in the
    # pending-rollback state SQLAlchemy leaves behind.
    assert session.in_transaction()
    with pytest.raises(PendingRollbackError):
        session.query(Topic).count()

    session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.query(Resource).count() == 0
    assert session.query(Event).filter(Event.event_type == "CALLER_EVENT").count() == 0
    assert session.query(Topic).count() == 0
