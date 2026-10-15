"""Knowledge refusals contracts for topics."""

from __future__ import annotations

import pytest
from organiser.services.context_knowledge import ContextKnowledgeError
from organiser.services.domain import (
    DomainValidationError,
)
from organiser.services.knowledge import KnowledgeContentError, KnowledgeError
from organiser.services.topic_knowledge import (
    _CONTENT_DEFECTS,
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeContentError,
    TopicKnowledgeError,
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


def test_a_content_defect_after_the_intent_still_reports_intent_and_no_completion(
    session, service, store, topic
):
    """The audit contract is unchanged for the newly classified errors.

    A structure that can only be discovered while structuring the document is
    found *after* the intent, so the normal intent/failure pair applies -- with the
    non-transient code.
    """
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    path.write_text("# Topic\n\n## Notizen\n\nn\n\n## notizen\n\n\u00e4\n", encoding="utf-8")
    with pytest.raises(TopicKnowledgeContentError):
        service.apply_topic_update(topic.id, text="Lernnotiz")
    types = event_types(session, topic)
    assert types[-2:] == ["TOPIC_KNOWLEDGE_WRITE_INTENT", "TOPIC_KNOWLEDGE_FAILED"]
    assert TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT not in types
    assert last_failure(session, topic).payload["code"] == "topic_knowledge_content_invalid"


def test_a_content_error_from_the_store_read_is_not_swallowed_by_the_generic_branch(
    session, service, store, topic, monkeypatch
):
    """The classification must be reachable, not dead code.

    ``KnowledgeContentError`` subclasses ``KnowledgeError``. An ``except`` clause
    listing the generic tuple first would therefore swallow every content defect and
    route it into the transient-looking branch -- which is exactly what this class
    exists to prevent. The store's read is the seam where such an error can arise.
    """
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    before = path.read_bytes()
    real_read = store._filesystem.read_guarded

    def refuse(*args, **kwargs):
        raise KnowledgeContentError("Abschnitt kommt mehrfach vor")

    monkeypatch.setattr(store._filesystem, "read_guarded", refuse)

    with pytest.raises(TopicKnowledgeContentError) as failure:
        service.apply_topic_update(topic.id, text="Lernnotiz")
    assert failure.value.code == "topic_knowledge_content_invalid"
    assert path.read_bytes() == before

    # The ensure path reads through the same seam and must classify it the same way.
    with pytest.raises(TopicKnowledgeContentError):
        service.ensure_topic_knowledge(topic.id)
    assert path.read_bytes() == before
    assert real_read is not refuse


# -- the classification cannot be routed around by the exception hierarchy ------


def test_the_content_class_survives_the_except_order_it_depends_on():
    """The order in the ``except`` clauses is load-bearing; the MRO is why.

    ``KnowledgeContentError`` **is** a ``KnowledgeError``, so any handler that lists
    the generic tuple first swallows every content defect and answers 503. And
    ``TopicKnowledgeContentError`` must not be a ``DomainValidationError``, because
    that one is caught even earlier at the API boundary and would report a broken
    *file* as a broken *request*.
    """
    assert issubclass(KnowledgeContentError, KnowledgeError)
    assert KnowledgeContentError in _CONTENT_DEFECTS
    assert issubclass(TopicKnowledgeContentError, TopicKnowledgeError)
    assert issubclass(TopicKnowledgeContentError, ContextKnowledgeError)
    assert not issubclass(TopicKnowledgeContentError, DomainValidationError)
    assert not issubclass(TopicKnowledgeContentError, TopicKnowledgeAmbiguousError)
    assert UnicodeError in _CONTENT_DEFECTS


def test_a_unicode_defect_from_the_write_seam_is_classified_not_generic(
    session, service, store, topic, monkeypatch
):
    """``UnicodeError`` is in the tuple because a byte sequence is never fixable by retry.

    The decode happens while the existing file is read, before anything of ours is
    written, so ``not_written`` is provable and the answer may be the non-transient
    one.
    """
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)
    before = path.read_bytes()
    real_write = store._filesystem.write

    def refuse(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "start byte")

    monkeypatch.setattr(store._filesystem, "write", refuse)

    with pytest.raises(TopicKnowledgeContentError) as failure:
        service.apply_topic_update(topic.id, text="Lernnotiz")

    assert failure.value.code == "topic_knowledge_content_invalid"
    assert path.read_bytes() == before
    assert real_write is not refuse
    # Still audited as a non-transient refusal of an already recorded intent.
    assert last_failure(session, topic).payload["outcome"] == "not_written"


def test_every_failure_record_refuses_an_automatic_retry(
    session, service, store, topic, monkeypatch
):
    """The audit payload, not just the prose, carries the no-retry contract.

    D and E read the audit log, not this module's docstrings. A failure record
    without the no-retry text would be read as "pending, try again".
    """
    service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, f"contexts/{topic.context_id}", topic)

    def refuse(*args, **kwargs):
        raise PermissionError("nur lesbar")

    monkeypatch.setattr(store._filesystem, "write", refuse)
    with pytest.raises(TopicKnowledgeAmbiguousError):
        service.apply_topic_update(topic.id, text="Lernnotiz")

    monkeypatch.undo()
    # A duplicate section is only found while structuring the document, i.e. *after*
    # the intent -- that is the variant that produces an auditable failure record.
    path.write_text("# Topic\n\n## Lernstand\n\na\n\n## lernstand\n\nb\n", encoding="utf-8")
    with pytest.raises(TopicKnowledgeContentError):
        service.apply_topic_update(topic.id, text="Lernnotiz")

    failures = [
        event for event in events(session, topic) if event.event_type == "TOPIC_KNOWLEDGE_FAILED"
    ]
    assert [event.payload["code"] for event in failures] == [
        "topic_knowledge_write_failed",
        "topic_knowledge_content_invalid",
    ]
    assert all("nicht automatisch wiederholt" in event.payload["recovery"] for event in failures)
