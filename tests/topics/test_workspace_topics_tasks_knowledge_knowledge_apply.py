"""Knowledge apply contracts for topics."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from conftest import make_settings
from organiser.db.models import Topic
from organiser.db.session import create_session_factory
from organiser.services.context_knowledge import ContextKnowledgeService
from organiser.services.topic_knowledge import (
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeError,
)
from organiser.services.topics import create_topic
from support.workspace_topics_tasks_knowledge import (
    session as session,
    store as store,
    service as service,
    context as context,
    topic as topic,
    topic_file as topic_file,
    events as events,
    event_types as event_types,
    fail_on_event_commit as fail_on_event_commit,
    last_failure as last_failure,
)


# -- the explicit session boundary --------------------------------------------


def test_refuses_entry_while_the_caller_transaction_is_open(session, service, topic):
    """Genau der Fall, den die Context-Knowledge-Grenze verhindern soll."""
    session.execute(select(Topic.id).where(Topic.id == topic.id)).one()
    assert session.in_transaction()
    with pytest.raises(TopicKnowledgeError, match="Application-Transaktion"):
        service.ensure_topic_knowledge(topic.id)


def test_refuses_entry_inside_defer_domain_commit(session, service, topic):
    session.info["defer_domain_commit"] = True
    try:
        with pytest.raises(TopicKnowledgeError, match="defer_domain_commit"):
            service.ensure_topic_knowledge(topic.id)
        with pytest.raises(TopicKnowledgeError, match="defer_domain_commit"):
            service.apply_topic_update(topic.id, text="x")
    finally:
        session.info.pop("defer_domain_commit")


def test_refuses_entry_with_pending_orm_state(session, service, context):
    """Auch ORM-Zustand ohne offene Transaktion wird abgelehnt.

    ``Session.add()`` oeffnet in SQLAlchemy 2.0 bereits eine Transaktion, deshalb
    greift in der Praxis meist die erste der beiden Pruefungen. Beide Wege fuehren
    zum selben Ergebnis: kein Dateizugriff.
    """
    session.add(
        Topic(context_id=context.id, title="Offen", title_key="offen", knowledge_key="topic-offen")
    )
    with pytest.raises(TopicKnowledgeError):
        service.ensure_topic_knowledge("irgendwas")


def test_the_session_is_usable_again_after_a_refusal(session, service, store, context, topic):
    topic_id = topic.id
    session.execute(select(Topic.id).where(Topic.id == topic.id)).one()
    with pytest.raises(TopicKnowledgeError):
        service.ensure_topic_knowledge(topic.id)
    session.rollback()
    # Rollback expires ORM instances. Reading topic.id would reopen a caller
    # transaction before entering the service, so use the captured identifier.
    result = service.ensure_topic_knowledge(topic_id)
    assert topic_file(store, result.knowledge_path, topic).is_file()


def test_the_services_own_read_is_never_committed_as_caller_work(
    session, service, tmp_path, context, topic
):
    """Fremder ORM-Zustand im selben Aufruf wird nie mitcommittet."""
    topic_id = topic.id
    session.info["defer_domain_commit"] = True
    create_topic(session, context_id=context.id, title="Nachtrag")
    session.info.pop("defer_domain_commit")
    session.flush()
    with pytest.raises(TopicKnowledgeError):
        service.ensure_topic_knowledge(topic_id)
    assert session.in_transaction()  # refusal did not roll back caller work
    # Es gab keinen Commit, also ist der Nachtrag nur in dieser Session sichtbar.
    other = create_session_factory(make_settings(tmp_path, "topic-knowledge.sqlite3"))()
    try:
        assert other.execute(select(Topic.title).where(Topic.title == "Nachtrag")).scalar() is None
    finally:
        other.close()
    session.rollback()
    assert service.ensure_topic_knowledge(topic_id).created is True


# -- failures stay open -------------------------------------------------------


def test_filesystem_failure_during_the_topic_write_is_contained(
    session, service, store, context, topic, monkeypatch
):
    ContextKnowledgeService(session, store).ensure_context_knowledge(context.id)

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(store._filesystem, "atomic_write", staticmethod(boom))
    with pytest.raises(TopicKnowledgeError):
        service.ensure_topic_knowledge(topic.id)
    monkeypatch.undo()
    # Der Context bleibt gebunden und gueltig; ein bewusster zweiter Lauf traegt.
    assert (
        ContextKnowledgeService(session, store).ensure_context_knowledge(context.id).changed
        is False
    )
    assert service.ensure_topic_knowledge(topic.id).created is True


def test_filesystem_failure_during_apply_is_ambiguous(
    session, service, store, context, topic, monkeypatch
):
    service.ensure_topic_knowledge(topic.id)

    def boom(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(store._filesystem, "atomic_write", staticmethod(boom))
    with pytest.raises(TopicKnowledgeAmbiguousError, match="nicht automatisch wiederholt"):
        service.apply_topic_update(topic.id, text="Kapitel 3")
    types = event_types(session, topic)
    assert "TOPIC_KNOWLEDGE_WRITE_INTENT" in types
    assert "TOPIC_KNOWLEDGE_FAILED" in types
    failed = [
        event for event in events(session, topic) if event.event_type == "TOPIC_KNOWLEDGE_FAILED"
    ][-1]
    assert failed.payload["outcome"] == "unknown"
    assert TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT not in types


def test_rejected_input_is_proven_not_written(session, service, store, context, topic):
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{context.id}", topic)
    before = path.read_bytes()
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, text="## Eigene Ueberschrift")
    assert path.read_bytes() == before


def test_database_failure_after_the_write_does_not_double_append(
    session, service, store, context, topic, monkeypatch
):
    """Der Abschluss-Commit scheitert nach dem Dateischreiben.

    Der Zustand bleibt offen und ungeklaert; die Datei wird nicht zurueckgerollt.
    Der bewusste Wiederholungslauf darf den Block nicht doppelt anhaengen.
    """
    ensure = service.ensure_topic_knowledge(topic.id)
    topic_id = topic.id
    fail_on_event_commit(session, monkeypatch, TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT)
    with pytest.raises(TopicKnowledgeAmbiguousError, match="Intent ohne Abschluss"):
        service.apply_topic_update(topic.id, text="Kapitel 4 gelesen")
    monkeypatch.undo()
    assert not session.in_transaction()
    assert not session.new

    path = topic_file(store, ensure.knowledge_path, topic)
    assert path.read_text(encoding="utf-8").count("Kapitel 4 gelesen") == 1
    # Audit-Zustand: Intent ohne Abschluss.
    assert TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT not in [
        event.event_type for event in events(session, topic)
    ]
    session.commit()  # end the caller's audit read, not the failed service write

    # Bewusster zweiter Versuch: der identische Block wird nicht doppelt gefuegt.
    retry = service.apply_topic_update(topic_id, text="Kapitel 4 gelesen")
    assert retry.changed is False
    assert path.read_text(encoding="utf-8").count("Kapitel 4 gelesen") == 1
