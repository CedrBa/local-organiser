"""Knowledge outcomes contracts for topics."""

from __future__ import annotations

import pytest
import hashlib
from sqlalchemy.orm import Session
from organiser.services.domain import (
    EventContext,
    NotFoundError,
)
from organiser.services.topic_knowledge import (
    TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeError,
    TopicKnowledgeService,
)
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


def test_ambiguous_state_is_never_retried_automatically(
    session, service, store, context, topic, monkeypatch
):
    service.ensure_topic_knowledge(topic.id)
    writes = {"count": 0}
    real_atomic = store._filesystem.atomic_write

    def counting_atomic(path, content):
        writes["count"] += 1
        return real_atomic(path, content)

    monkeypatch.setattr(store._filesystem, "atomic_write", staticmethod(counting_atomic))
    fail_on_event_commit(session, monkeypatch, TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT)
    with pytest.raises(TopicKnowledgeAmbiguousError):
        service.apply_topic_update(topic.id, text="Kapitel 5")
    # Genau ein Dateischreibversuch: kein stiller zweiter Versuch, kein Rollback.
    assert writes["count"] == 1


# -- audit --------------------------------------------------------------------


def test_intent_precedes_the_completion(session, service, topic):
    service.ensure_topic_knowledge(topic.id)
    types = event_types(session, topic)
    assert types.index("TOPIC_KNOWLEDGE_INIT_INTENT") < types.index(
        TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT
    )


def test_every_operation_carries_its_own_operation_id(session, service, topic):
    service.ensure_topic_knowledge(topic.id)
    service.ensure_topic_knowledge(topic.id)
    ids = [
        event.payload["operation_id"]
        for event in events(session, topic)
        if event.event_type.startswith("TOPIC_KNOWLEDGE_")
    ]
    assert len(ids) == 4  # je Lauf Intent und Abschluss/No-op
    assert len(set(ids)) == 2  # und jeder Lauf hat seine eigene Kennung


def test_audit_payload_has_no_absolute_path(session, service, topic):
    service.ensure_topic_knowledge(topic.id)
    for event in events(session, topic):
        payload = str(event.payload)
        assert str(service.store.root) not in payload


def test_unknown_topic_read_leaves_owned_session_clean(session, service):
    with pytest.raises(NotFoundError):
        service.ensure_topic_knowledge("missing")
    assert not session.in_transaction()
    assert not session.new


def test_clean_external_connection_session_is_refused(session, store, topic):
    topic_id = topic.id
    with session.get_bind().begin() as connection:
        with Session(bind=connection) as caller:
            assert not caller.in_transaction()
            with pytest.raises(TopicKnowledgeError, match="Connection"):
                TopicKnowledgeService(caller, store).ensure_topic_knowledge(topic_id)
            assert connection.in_transaction()
    assert not list(store.root.rglob("*.md"))


@pytest.mark.parametrize("percent", [True, 1.5, "50"])
def test_direct_service_rejects_non_integer_percent_before_audit(session, service, topic, percent):
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, percent=percent)
    assert event_types(session, topic) == ["TOPIC_CREATED"]


def test_direct_service_enforces_text_budget(service, topic):
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, text="a" * 20_001)


@pytest.mark.parametrize("operation", ["ensure", "apply"])
def test_completion_requires_persistent_content(
    session, service, store, topic, monkeypatch, operation
):
    topic_id = topic.id
    if operation == "apply":
        service.ensure_topic_knowledge(topic_id)
    real_write = store._filesystem.write

    def corrupt(path, content):
        write = real_write(path, content)
        path.write_text("changed outside the service\n", encoding="utf-8")
        return write

    monkeypatch.setattr(store._filesystem, "write", corrupt)
    with pytest.raises(TopicKnowledgeAmbiguousError):
        if operation == "ensure":
            service.ensure_topic_knowledge(topic_id)
        else:
            service.apply_topic_update(topic_id, text="not confirmed")
    assert not session.in_transaction()
    types = event_types(session, topic)
    completion = (
        TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT
        if operation == "ensure"
        else TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT
    )
    assert completion not in types
    assert "TOPIC_KNOWLEDGE_FAILED" in types


def test_ensure_completion_commit_failure_is_ambiguous_and_repairable(
    session, service, store, topic, monkeypatch
):
    topic_id = topic.id
    knowledge_key = topic.knowledge_key
    fail_on_event_commit(session, monkeypatch, TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT)
    with pytest.raises(TopicKnowledgeAmbiguousError):
        service.ensure_topic_knowledge(topic_id)
    assert not session.in_transaction() and not session.new
    files = list(store.root.rglob(f"{knowledge_key}.md"))
    assert len(files) == 1
    before = files[0].read_bytes()
    monkeypatch.undo()
    result = service.ensure_topic_knowledge(topic_id)
    assert result.created is False and files[0].read_bytes() == before


def test_intent_commit_failure_writes_no_topic_file(session, service, store, topic, monkeypatch):
    fail_on_event_commit(session, monkeypatch, "TOPIC_KNOWLEDGE_INIT_INTENT")
    with pytest.raises(TopicKnowledgeError):
        service.ensure_topic_knowledge(topic.id)
    assert not session.in_transaction() and not session.new
    assert not list(store.root.rglob("*.md"))


def test_error_after_file_replace_stays_ambiguous_without_retry(
    session, service, store, topic, monkeypatch
):
    ensured = service.ensure_topic_knowledge(topic.id)
    real_write = store._filesystem.atomic_write

    def write_then_fail(path, content):
        real_write(path, content)
        raise OSError("late fsync failure with private path " + str(path))

    monkeypatch.setattr(store._filesystem, "atomic_write", write_then_fail)
    with pytest.raises(TopicKnowledgeAmbiguousError):
        service.apply_topic_update(topic.id, text="saved once")
    assert not session.in_transaction()
    assert topic_file(store, ensured.knowledge_path, topic).read_text().count("saved once") == 1
    failure = events(session, topic)[-1]
    assert failure.payload["outcome"] == "unknown"
    assert str(store.root) not in str(failure.payload)
    assert TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT not in event_types(session, topic)


def test_audit_hash_and_proposal_provenance_are_verified(session, service, store, topic):
    result = service.ensure_topic_knowledge(topic.id)
    write = service.apply_topic_update(
        topic.id,
        text="Erster Absatz\n\nZweiter Absatz",
        context=EventContext(source_type="context_action_proposal", source_id="proposal-1"),
    )
    assert (
        write.content_hash
        == hashlib.sha256(topic_file(store, result.knowledge_path, topic).read_bytes()).hexdigest()
    )
    record = events(session, topic)[-1]
    assert (record.source_type, record.source_id) == ("context_action_proposal", "proposal-1")
    session.commit()
    assert (
        service.apply_topic_update(topic.id, text="Erster Absatz\n\nZweiter Absatz").changed
        is False
    )


def test_duplicate_learning_sections_are_refused_without_content_loss(
    session, service, store, topic
):
    result = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, result.knowledge_path, topic)
    path.write_text("# Topic\n\n## Lernstand\n\na\n\n## lernstand\n\nb\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, percent=75)
    assert path.read_bytes() == before
    assert events(session, topic)[-1].payload["outcome"] == "not_written"
