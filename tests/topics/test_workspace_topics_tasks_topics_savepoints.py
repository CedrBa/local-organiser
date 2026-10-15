"""Savepoints contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from organiser.db.models import Context, Topic
from organiser.services.domain import (
    ConflictError,
)
from organiser.services.topics import (
    _title_key_uniqueness_violated,
    _title_uniqueness_boundary,
    _real_transaction_is_open,
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


# -- the SAVEPOINT hazard, pinned -----------------------------------------------


def test_no_savepoint_is_opened_while_the_driver_is_in_autocommit(session, tmp_path, context_a):
    """The guard is observable from the outside, not just asserted in a docstring."""
    assert not _real_transaction_is_open(session)

    with _title_uniqueness_boundary(session):
        assert not session.in_nested_transaction()

    # And a successful boundary commits nothing while there is no transaction.
    with _title_uniqueness_boundary(session):
        session.add(a_topic(context_a.id, title="Neu", title_key="neu"))
        session.flush()
    with observer_factory(tmp_path)() as other:
        assert other.query(Topic).count() == 0


def test_an_unconditional_savepoint_would_commit_the_callers_rows(session, tmp_path, context_a):
    """Why the guard exists: measured on this engine, not assumed.

    ``begin_nested()`` while the driver is in autocommit *starts* a transaction, and
    ``RELEASE SAVEPOINT`` then commits it. The row below is visible to a second
    connection right after the block and *survives* ``session.rollback()``. A
    ``Session.in_transaction()`` check would not have prevented this -- it is already
    ``True`` after a bare ``SELECT``.
    """
    session.scalar(select(Context.id).where(Context.id == context_a.id))
    assert session.in_transaction() and not _real_transaction_is_open(session)

    with session.begin_nested():
        session.add(a_topic(context_a.id, title="Gefahr", title_key="gefahr"))

    with observer_factory(tmp_path)() as other:
        assert [row.title for row in other.query(Topic)] == ["Gefahr"]
    session.rollback()
    with observer_factory(tmp_path)() as other:
        assert [row.title for row in other.query(Topic)] == ["Gefahr"]


def test_the_boundary_uses_a_savepoint_once_a_real_transaction_exists(session, context_a):
    add_caller_work(session, context_a.id, flush=True)
    assert _real_transaction_is_open(session)

    with _title_uniqueness_boundary(session):
        assert session.in_nested_transaction()

    assert not session.in_nested_transaction()


def test_real_transaction_is_open_fails_closed_for_an_unreachable_connection():
    """An unknown driver must not be read as "a transaction is open"."""

    class Unusable:
        def connection(self):
            raise RuntimeError("keine Verbindung")

    assert _real_transaction_is_open(Unusable()) is False


# -- the pre-check order differs per operation, on purpose ----------------------


def test_create_is_decided_by_the_database_but_rename_by_the_precheck(session, context_a):
    """Both orders are deliberate; the tests must not quietly unify them.

    ``create_topic`` flushes *after* its duplicate ``SELECT``, so a staged collision
    is only caught by ``uq_topics_context_title_key`` -- the ``ConflictError`` has an
    ``IntegrityError`` cause. ``update_topic`` flushes *before* its clash ``SELECT``,
    so the same staged collision is refused by the pre-check and there is no cause.
    """
    session.info["defer_domain_commit"] = True
    pending = add_pending_colliding_topic(session, context_a.id, "Gamma", "gamma")
    assert pending in session.new  # staged, deliberately not flushed

    with pytest.raises(ConflictError) as created:
        create_topic(session, context_id=context_a.id, title="Gamma")
    assert isinstance(created.value.__cause__, IntegrityError)

    session.info.pop("defer_domain_commit")
    renamed = create_topic(session, context_id=context_a.id, title="Alpha")
    session.commit()
    session.info["defer_domain_commit"] = True
    staged = add_pending_colliding_topic(session, context_a.id, "Zeta", "zeta")

    with pytest.raises(ConflictError) as renamed_failure:
        update_topic(session, topic_id=renamed.id, title="Zeta")
    assert renamed_failure.value.__cause__ is None
    # Same input state, different refusal path -- and the rename itself is untouched.
    assert session.get(Topic, renamed.id).title == "Alpha"
    assert session.get(Topic, staged.id).title_key == "zeta"


def test_a_foreign_integrity_failure_inside_the_boundary_is_not_a_duplicate_title(
    session, tmp_path, context_a
):
    """Finding: the classification gate had no test that could fail.

    Only the title-key violation ever reached the gate before, because every other
    way to make the flush fail happens in the caller-owned pre-flush or in the
    boundary helper directly. This one is raised *inside* ``update_topic``'s boundary
    by the database, on another table entirely.
    """
    topic = create_topic(session, context_id=context_a.id, title="Alpha")
    install_failing_audit_trigger(session)

    with pytest.raises(IntegrityError) as failure:
        update_topic(session, topic_id=topic.id, title="Beta")

    assert not isinstance(failure.value, ConflictError)
    assert not _title_key_uniqueness_violated(failure.value)
    assert "externer Audit-Fehler" in str(failure.value.orig)
    # And the rename really did not happen behind the failure.
    session.rollback()
    with observer_factory(tmp_path)() as other:
        assert other.get(Topic, topic.id).title == "Alpha"
