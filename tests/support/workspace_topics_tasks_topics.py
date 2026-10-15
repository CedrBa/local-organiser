"""Shared fixtures and builders for test_workspace_topics_tasks_topics contracts; no executable tests."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Event, Resource, Topic, new_id
from organiser.db.session import create_session_factory
from organiser.domain.topics import (
    knowledge_key_for,
)
from organiser.services.domain import (
    create_context,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "topics.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def context_a(session):
    return create_context(session, title="Finanzierung", type="course")


@pytest.fixture
def context_b(session):
    return create_context(session, title="Buchhaltung", type="course")


def make_resource(session, context, title="Dokument") -> Resource:
    resource = Resource(
        context_id=context.id,
        locator=f"/tmp/{new_id()}",
        relative_path=title,
        source_path_snapshot=f"/tmp/{new_id()}",
        title=title,
        resource_type="markdown",
    )
    session.add(resource)
    session.flush()
    return resource


# -- review follow-up: exact no-op semantics of update_topic -------------------


def write_non_canonical_topic(session, context_id, *, title, title_key):
    """A topic row whose stored title_key is not the folding of its title.

    Such a row can only come from history or from a direct/manual write. It is the
    case that decides whether an empty PATCH is allowed to touch the row.
    """
    identifier = new_id()
    topic = Topic(
        id=identifier,
        context_id=context_id,
        title=title,
        title_key=title_key,
        status="not_started",
        knowledge_key=knowledge_key_for(identifier),
    )
    session.add(topic)
    session.flush()
    return topic


def stored_topic_row(session, topic_id):
    """Read the persisted row as columns, bypassing the identity map.

    SQLite hands ``DateTime(timezone=True)`` back without tzinfo, so comparing an
    in-memory attribute with a re-read value would compare representations, not
    facts. Reading both sides the same way keeps the assertion about the row.
    """
    row = session.execute(
        select(Topic.title, Topic.title_key, Topic.status, Topic.updated_at).where(
            Topic.id == topic_id
        )
    ).one()
    return dict(zip(("title", "title_key", "status", "updated_at"), row, strict=True))


def insert_rival_topic(tmp_path, context_id, *, title, title_key):
    """Commit a competing topic through a second connection, winning the race."""
    rival_id = new_id()
    with create_session_factory(make_settings(tmp_path, "topics.sqlite3"))() as rival:
        rival.add(
            Topic(
                id=rival_id,
                context_id=context_id,
                title=title,
                title_key=title_key,
                status="not_started",
                knowledge_key=knowledge_key_for(rival_id),
            )
        )
        rival.commit()
    return rival_id


# -- secondpass: a constraint violation must never destroy the caller's state ---


def driver_transaction_is_open(session) -> bool:
    """The real SQLite state, not SQLAlchemy's logical ``in_transaction()``.

    A read opens no SQLite transaction, so the two disagree exactly where the
    SAVEPOINT decision matters.
    """
    return bool(getattr(session.connection().connection, "in_transaction", False))


def observer_factory(tmp_path):
    return create_session_factory(make_settings(tmp_path, "topics.sqlite3"))


def add_caller_work(session, context_id, *, flush):
    """A ``Resource`` plus an ``Event``: what E writes before calling a domain action.

    ``flush=False`` leaves them pending in the session, ``flush=True`` writes them
    into an open transaction. Both flavours have to survive a refused operation.
    """
    resource = Resource(
        context_id=context_id,
        locator=f"/tmp/{new_id()}",
        relative_path="caller.md",
        source_path_snapshot=f"/tmp/{new_id()}",
        title="Caller-Arbeit",
        resource_type="markdown",
    )
    event = Event(
        event_type="CALLER_EVENT", entity_type="context", entity_id=context_id, payload={}
    )
    session.add_all([resource, event])
    if flush:
        session.flush()
    return resource, event


def add_pending_colliding_topic(session, context_id, title, title_key):
    """A topic row the caller has created but **not** flushed.

    The session factory runs ``autoflush=False``, so the pre-check SELECT inside a
    domain function neither flushes nor sees this row. It is written together with
    the operation's own row, and ``uq_topics_context_title_key`` then produces a
    real ``IntegrityError``. This is the only reachable route to the constraint path
    in the SQLite single-writer MVP, and it is a genuine one -- a caller that staged
    its work in the same transaction is exactly E's situation.
    """
    identifier = new_id()
    topic = Topic(
        id=identifier,
        context_id=context_id,
        title=title,
        title_key=title_key,
        status="not_started",
        knowledge_key=knowledge_key_for(identifier),
    )
    session.add(topic)
    assert topic in session.new
    return topic


def assert_caller_work_intact(session, observer, resource, event, *, topics):
    """Caller rows still in the session, still invisible outside, nothing extra."""
    assert session.get(Resource, resource.id) is not None
    assert session.query(Event).filter(Event.id == event.id).count() == 1
    assert session.query(Topic).count() == topics
    with observer() as other:
        assert other.get(Resource, resource.id) is None
        assert other.query(Event).filter(Event.id == event.id).count() == 0


def insert_duplicate_titles(session, context_id, title, title_key):
    """Two inserts of one ``title_key`` -> a real ``IntegrityError``.

    The same violation ``create_topic`` meets, produced by the same
    ``topics.uq_topics_context_title_key`` constraint. Nothing is faked.
    """
    for _ in range(2):
        identifier = new_id()
        session.add(
            Topic(
                id=identifier,
                context_id=context_id,
                title=title,
                title_key=title_key,
                status="not_started",
                knowledge_key=knowledge_key_for(identifier),
            )
        )
    session.flush()


# -- the boundary is for ONE constraint, not for IntegrityError in general -----


def a_topic(context_id, *, title, title_key, knowledge_key=None):
    """A hand-built topic row: ``knowledge_key`` defaults to the derived, legal one."""
    identifier = new_id()
    return Topic(
        id=identifier,
        context_id=context_id,
        title=title,
        title_key=title_key,
        status="not_started",
        knowledge_key=knowledge_key or knowledge_key_for(identifier),
    )


def install_failing_audit_trigger(session) -> None:
    """A real, *foreign* ``IntegrityError`` raised inside the boundary's own flush.

    ``RAISE(ABORT, ...)`` in a trigger produces a genuine
    ``sqlite3.IntegrityError`` from the database, on the ``events`` table -- a
    different constraint from the ``topics`` one the boundary is about. Nothing here
    is simulated or monkeypatched; the driver raises it.
    """
    session.execute(
        text(
            "CREATE TRIGGER fail_topic_updated BEFORE INSERT ON events "
            "WHEN NEW.event_type = 'TOPIC_UPDATED' "
            "BEGIN SELECT RAISE(ABORT, 'externer Audit-Fehler'); END"
        )
    )
    session.commit()
