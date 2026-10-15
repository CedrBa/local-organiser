"""Shared fixtures and builders for test_workspace_topics_tasks_knowledge contracts; no executable tests."""

from __future__ import annotations

import pytest
from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    create_context,
    list_events_for_entity,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.topic_knowledge import (
    TopicKnowledgeService,
)
from organiser.services.topics import create_topic


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "topic-knowledge.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def store(knowledge_root):
    return KnowledgeStore(knowledge_root)


@pytest.fixture
def service(session, store):
    return TopicKnowledgeService(session, store)


@pytest.fixture
def context(session):
    return create_context(session, title="Finanzierung", type="course")


@pytest.fixture
def topic(session, context):
    return create_topic(session, context_id=context.id, title="Bilanzierung")


def topic_file(store, knowledge_path, topic):
    return store.root / knowledge_path / "topics" / f"{topic.knowledge_key}.md"


def events(session, topic):
    return list_events_for_entity(session, entity_type="topic", entity_id=topic.id)


def event_types(session, topic):
    return [event.event_type for event in events(session, topic)]


def fail_on_event_commit(session, monkeypatch, event_type: str):
    """Armiere einen Commit, der genau beim Abschluss eines Ereignistyps scheitert.

    Gezielt nach Ereignistyp statt nach Aufrufzähler: ``apply_topic_knowledge``
    committet an mehreren Stellen (Bindungs-Read, Intent, Abschluss), und eine
    Zählung wäre an der Reihenfolge des SQLAlchemy-Verhaltens zerbrechlich.
    """
    real_commit = session.commit

    def guarded_commit():
        pending = [item for item in session.new if type(item).__name__ == "Event"]
        if any(item.event_type == event_type for item in pending):
            raise RuntimeError("Datenbank nicht erreichbar")
        return real_commit()

    monkeypatch.setattr(session, "commit", guarded_commit)
    return real_commit


# -- review follow-up: content defects are not transient ------------------------


def last_failure(session, topic):
    return [
        event for event in events(session, topic) if event.event_type == "TOPIC_KNOWLEDGE_FAILED"
    ][-1]
