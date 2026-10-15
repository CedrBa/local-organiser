"""Knowledge binding contracts for topics."""

from __future__ import annotations

from organiser.services.topic_knowledge import (
    TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_INIT_NOOP_EVENT,
)
from organiser.services.topics import create_topic, set_topic_status, update_topic
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


# -- the address is the database key ------------------------------------------


def test_ensure_creates_the_file_at_the_database_address(session, service, store, context, topic):
    result = service.ensure_topic_knowledge(topic.id)
    assert result.knowledge_key == topic.knowledge_key
    assert result.relative_file == f"topics/{topic.knowledge_key}.md"
    assert result.created is True
    assert result.context_bound_now is True
    assert topic_file(store, result.knowledge_path, topic).is_file()


def test_the_address_does_not_contain_the_title(session, service, store, context, topic):
    service.ensure_topic_knowledge(topic.id)
    files = list((store.root).rglob("topics/*.md"))
    assert [item.name for item in files] == [f"{topic.knowledge_key}.md"]
    assert "bilanzierung.md" not in [item.name for item in files]


def test_rename_does_not_move_or_rewrite_the_file(session, service, store, context, topic):
    first = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, first.knowledge_path, topic)
    before = path.read_text(encoding="utf-8")
    update_topic(session, topic_id=topic.id, title="Bilanzierung Teil 2")
    assert topic.knowledge_key == first.knowledge_key
    assert path.is_file()
    assert path.read_text(encoding="utf-8") == before
    # Auch der alte Slug-Namen wird nicht nachtraeglich erzeugt.
    assert not (store.root / first.knowledge_path / "topics" / "bilanzierung-teil-2.md").exists()


def test_two_topics_never_share_a_file(session, service, store, context):
    first = create_topic(session, context_id=context.id, title="Zinsen")
    second = create_topic(session, context_id=context.id, title="Abschreibung")
    a = service.ensure_topic_knowledge(first.id)
    b = service.ensure_topic_knowledge(second.id)
    assert a.relative_file != b.relative_file
    assert len(list((store.root / a.knowledge_path / "topics").glob("*.md"))) == 2


def test_legacy_slug_file_is_neither_adopted_nor_overwritten(
    session, service, store, context, topic
):
    """Altdateien bleiben Legacy-Knowledge und werden nie angefasst."""
    service.ensure_topic_knowledge(topic.id)
    knowledge_path = f"contexts/{context.id}"
    legacy = store.root / knowledge_path / "topics" / "bilanzierung.md"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text("## Notizen\n\nalte, handschriftliche Notiz\n", encoding="utf-8")
    before = legacy.read_bytes()

    result = service.ensure_topic_knowledge(topic.id)
    # Der Topic hat seine eigene DB-Adresse; die Legacy-Datei wird weder
    # umbenannt noch ueberschrieben und nicht als Initialisierung akzeptiert.
    assert result.created is False
    assert legacy.read_bytes() == before
    assert topic_file(store, result.knowledge_path, topic).is_file()


def test_placeholder_mentions_the_title_but_no_topic_status(
    session, service, store, context, topic
):
    result = service.ensure_topic_knowledge(topic.id)
    content = topic_file(store, result.knowledge_path, topic).read_text(encoding="utf-8")
    assert "Bilanzierung" in content
    assert content.startswith("# Bilanzierung")
    # Der DB-Status darf nicht als kanonischer Markdown-Status gespiegelt werden.
    for status in ("not_started", "in_progress", "done", "blocked"):
        assert status not in content


def test_changing_the_topic_status_does_not_touch_the_file(session, service, store, context, topic):
    result = service.ensure_topic_knowledge(topic.id)
    path = topic_file(store, result.knowledge_path, topic)
    before = path.read_bytes()
    set_topic_status(session, topic_id=topic.id, status="done")
    assert path.read_bytes() == before


# -- ensure is explicit, repeatable and never destructive ---------------------


def test_ensure_is_idempotent_and_reports_a_noop(session, service, store, context, topic):
    first = service.ensure_topic_knowledge(topic.id)
    before = topic_file(store, first.knowledge_path, topic).read_bytes()
    second = service.ensure_topic_knowledge(topic.id)
    assert second.created is False
    assert topic_file(store, second.knowledge_path, topic).read_bytes() == before
    types = event_types(session, topic)
    assert types.count(TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT) == 1
    assert types.count(TOPIC_KNOWLEDGE_INIT_NOOP_EVENT) == 1
