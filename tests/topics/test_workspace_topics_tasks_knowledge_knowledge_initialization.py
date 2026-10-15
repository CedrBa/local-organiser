"""Knowledge initialization contracts for topics."""

from __future__ import annotations

import pytest
from organiser.services.context_knowledge import ContextKnowledgeService
from organiser.services.domain import (
    NotFoundError,
)
from organiser.services.topic_knowledge import (
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT,
    TopicKnowledgeError,
    TopicKnowledgeNotInitializedError,
    TopicKnowledgeService,
)
from organiser.services.topics import create_topic, set_topic_status
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


def test_ensure_repairs_a_deleted_file(session, service, store, context, topic):
    first = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, first.knowledge_path, topic)
    path.unlink()
    second = service.ensure_topic_knowledge(topic.id)
    assert second.created is True
    assert path.is_file()


def test_ensure_never_overwrites_hand_edited_content(session, service, store, context, topic):
    first = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, first.knowledge_path, topic)
    path.write_text("# Bilanzierung\n\n## Notizen\n\nvon Hand ergaenzt\n", encoding="utf-8")
    result = service.ensure_topic_knowledge(topic.id)
    assert result.created is False
    assert "von Hand ergaenzt" in path.read_text(encoding="utf-8")


def test_unknown_topic_raises_before_any_filesystem_effect(session, service, store, context):
    with pytest.raises(NotFoundError):
        service.ensure_topic_knowledge("missing")
    assert not list(store.root.rglob("*.md"))


def test_topic_creation_alone_creates_no_file(session, store, context):
    create_topic(session, context_id=context.id, title="Bilanzierung")
    assert not list(store.root.rglob("*.md"))


# -- apply --------------------------------------------------------------------


def test_apply_appends_text_and_keeps_the_file_address(session, service, store, context, topic):
    ensure = service.ensure_topic_knowledge(topic.id)
    write = service.apply_topic_update(topic.id, text="Kapitel 2 gelesen", percent=75)
    assert write.relative_file == ensure.relative_file
    assert write.knowledge_key == topic.knowledge_key
    assert write.changed is True
    content = topic_file(store, ensure.knowledge_path, topic).read_text(encoding="utf-8")
    assert "Kapitel 2 gelesen" in content
    assert "- Fortschritt: 75 %" in content


def test_apply_writes_no_topic_status_into_markdown(session, service, store, context, topic):
    ensure = service.ensure_topic_knowledge(topic.id)
    set_topic_status(session, topic_id=topic.id, status="blocked")
    service.apply_topic_update(topic.id, text="Hinweis", percent=10)
    content = topic_file(store, ensure.knowledge_path, topic).read_text(encoding="utf-8")
    assert "blocked" not in content


def test_apply_requires_an_existing_file(session, service, store, context, topic):
    with pytest.raises(TopicKnowledgeNotInitializedError):
        service.apply_topic_update(topic.id, text="zu frueh")
    assert not list(store.root.rglob("*.md"))


def test_apply_requires_a_complete_context_store(session, store, topic):
    """Teilinitialisierung: apply legt nichts an, ensure repariert."""
    service = TopicKnowledgeService(session, store)
    with pytest.raises(TopicKnowledgeNotInitializedError):
        service.apply_topic_update(topic.id, text="x")
    repaired = service.ensure_topic_knowledge(topic.id)
    assert repaired.created is True


def test_partial_core_structure_is_refused_and_creates_nothing(
    session, service, store, context, topic
):
    ContextKnowledgeService(session, store).ensure_context_knowledge(context.id)
    knowledge_path = f"contexts/{context.id}"
    (store.root / knowledge_path / "progress.md").unlink()
    with pytest.raises(TopicKnowledgeNotInitializedError):
        service.apply_topic_update(topic.id, text="nichts geschrieben")
    assert not topic_file(store, knowledge_path, topic).exists()


def test_apply_repeating_the_same_block_is_a_noop(session, service, store, context, topic):
    ensure = service.ensure_topic_knowledge(topic.id)
    service.apply_topic_update(topic.id, text="Kapitel 2 gelesen")
    service.apply_topic_update(topic.id, text="Kapitel 2 gelesen")
    content = topic_file(store, ensure.knowledge_path, topic).read_text(encoding="utf-8")
    assert content.count("Kapitel 2 gelesen") == 1
    types = event_types(session, topic)
    assert types.count(TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT) == 1
    assert types.count(TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT) == 1


def test_apply_needs_at_least_one_field(session, service, context, topic):
    service.ensure_topic_knowledge(topic.id)
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id)


@pytest.mark.parametrize("percent", [-1, 101])
def test_apply_rejects_percent_outside_the_range(session, service, context, topic, percent):
    service.ensure_topic_knowledge(topic.id)
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, percent=percent)


def test_apply_rejects_ambiguous_section_text(session, service, context, topic):
    service.ensure_topic_knowledge(topic.id)
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, text="## Eigene Ueberschrift")


def test_apply_rejects_text_that_would_open_a_code_fence(session, service, context, topic):
    service.ensure_topic_knowledge(topic.id)
    with pytest.raises(TopicKnowledgeError):
        service.apply_topic_update(topic.id, text="```python\nprint(1)\n")


def test_apply_only_needs_text_or_percent(session, service, store, context, topic):
    ensure = service.ensure_topic_knowledge(topic.id)
    service.apply_topic_update(topic.id, percent=50)
    assert "- Fortschritt: 50 %" in topic_file(store, ensure.knowledge_path, topic).read_text(
        encoding="utf-8"
    )
