"""Knowledge coordination contracts for topics."""

from __future__ import annotations

import pytest
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from sqlalchemy.orm import Session
from conftest import make_settings
from organiser.db.models import Event
from organiser.db.session import create_session_factory
from organiser.services.topic_knowledge import (
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeContentError,
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


def test_shared_store_serializes_concurrent_topic_updates(
    session, service, store, topic, monkeypatch
):
    topic_id = topic.id
    ensured = service.ensure_topic_knowledge(topic_id)
    path = topic_file(store, ensured.knowledge_path, topic)
    engine = session.get_bind()
    first_write, second_started, second_read, release = (threading.Event() for _ in range(4))
    marker = threading.local()
    original_write, original_read = store._filesystem.atomic_write, store._filesystem.read_guarded

    def delayed_write(target, content):
        if getattr(marker, "name", None) == "first" and target == path:
            first_write.set()
            assert release.wait(5)
        return original_write(target, content)

    def observed_read(target):
        if getattr(marker, "name", None) == "second" and target == path:
            second_read.set()
        return original_read(target)

    def worker(name):
        marker.name = name
        if name == "second":
            second_started.set()
        with Session(engine, expire_on_commit=False, autoflush=False) as local:
            return TopicKnowledgeService(local, store).apply_topic_update(topic_id, text=name)

    monkeypatch.setattr(store._filesystem, "atomic_write", delayed_write)
    monkeypatch.setattr(store._filesystem, "read_guarded", observed_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker, "first")
        try:
            assert first_write.wait(5)
            second = pool.submit(worker, "second")
            assert second_started.wait(5)
            assert not second_read.wait(0.1)
        finally:
            release.set()
        assert first.result(timeout=5).changed
        assert second.result(timeout=5).changed
    content = path.read_text(encoding="utf-8")
    assert content.count("first") == 1 and content.count("second") == 1


def test_existing_crlf_topic_file_is_preserved_and_hashed_as_bytes(session, service, store, topic):
    result = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, result.knowledge_path, topic)
    content = b"# Handwritten\r\n\r\n## Notizen\r\n\r\nunchanged\r\n"
    path.write_bytes(content)
    repeated = service.ensure_topic_knowledge(topic.id)
    assert not repeated.created and path.read_bytes() == content
    write = service.apply_topic_update(topic.id, text="unchanged")
    assert not write.changed and path.read_bytes() == content
    assert write.content_hash == hashlib.sha256(content).hexdigest()


def test_duplicate_managed_sections_are_a_non_transient_content_error(
    session, service, store, topic
):
    """A user-fixable file must not look like "try again later"."""
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    path.write_text("# Topic\n\n## Lernstand\n\na\n\n## lernstand\n\nb\n", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(TopicKnowledgeContentError) as failure:
        service.apply_topic_update(topic.id, percent=75)

    # Not ambiguous, not a generic failure: nobody should retry this.
    assert not isinstance(failure.value, TopicKnowledgeAmbiguousError)
    assert failure.value.code == "topic_knowledge_content_invalid"
    assert path.read_bytes() == before
    failure_event = last_failure(session, topic)
    assert (failure_event.payload["code"], failure_event.payload["outcome"]) == (
        "topic_knowledge_content_invalid",
        "not_written",
    )
    assert TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT not in event_types(session, topic)


def test_a_topic_file_that_is_not_utf8_is_a_non_transient_content_error(
    session, service, store, topic
):
    """Invalid UTF-8 is decided by reading, so nothing was written -- provably."""
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    path.write_bytes(b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n")
    before = path.read_bytes()

    with pytest.raises(TopicKnowledgeContentError) as failure:
        service.apply_topic_update(topic.id, text="Lernnotiz")

    assert not isinstance(failure.value, TopicKnowledgeAmbiguousError)
    assert path.read_bytes() == before
    # The decode fails while reading, before an intent exists: nothing ran, so
    # there is nothing to audit and no ``needs_review`` state to resolve.
    assert "TOPIC_KNOWLEDGE_FAILED" not in event_types(session, topic)


def test_ensure_also_refuses_a_non_utf8_topic_file_as_non_transient(
    session, service, store, topic, tmp_path
):
    """The same classification on the ensure path, which also reads before writing."""
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    path.write_bytes(b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n")
    before = path.read_bytes()

    with pytest.raises(TopicKnowledgeContentError) as failure:
        service.ensure_topic_knowledge(topic.id)

    assert not isinstance(failure.value, TopicKnowledgeAmbiguousError)
    assert path.read_bytes() == before
    # Unlike ``apply``, ensure records its intent first, so the refusal is a
    # durable intent/failure pair -- with the non-transient code and no completion.
    with create_session_factory(make_settings(tmp_path, "topic-knowledge.sqlite3"))() as reader:
        recorded = [
            event
            for event in reader.query(Event).filter(Event.event_type.like("TOPIC_KNOWLEDGE%")).all()
        ]
    types = [event.event_type for event in recorded]
    # The first ensure completed; the second one is the refused attempt.
    assert types[:2] == ["TOPIC_KNOWLEDGE_INIT_INTENT", "TOPIC_KNOWLEDGE_INIT_COMPLETED"]
    assert types[2:] == ["TOPIC_KNOWLEDGE_INIT_INTENT", "TOPIC_KNOWLEDGE_FAILED"]
    assert (recorded[-1].payload["code"], recorded[-1].payload["outcome"]) == (
        "topic_knowledge_content_invalid",
        "not_written",
    )


def test_a_content_defect_still_keeps_the_session_usable(session, service, store, topic):
    """A refused write must not leave the owned session wedged."""
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    path.write_bytes(b"# Topic\n\n## Notizen\n\n\xff\xfe kaputt\n")
    with pytest.raises(TopicKnowledgeContentError):
        service.apply_topic_update(topic.id, text="Lernnotiz")

    path.write_text("# Topic\n\n## Notizen\n\nwieder lesbar\n", encoding="utf-8")
    assert service.apply_topic_update(topic.id, text="Lernnotiz").changed is True
