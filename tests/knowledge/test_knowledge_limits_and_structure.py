"""Limits and structure contracts for knowledge."""

from __future__ import annotations

import os
from pathlib import Path
import pytest
from organiser.services.knowledge import (
    KnowledgeContentError,
    KnowledgeStore,
    UnsafeKnowledgePathError,
)
from support.knowledge import (
    MATHE as MATHE,
    STATISTIK as STATISTIK,
    _write_core_file as _write_core_file,
)


def test_progress_without_sections_and_empty_topic_fallbacks(knowledge_store, tmp_path):
    knowledge_store.ensure_context_knowledge(MATHE)
    (knowledge_store.context_directory(MATHE) / "progress.md").write_text(
        "# Alt\n", encoding="utf-8"
    )

    progress = knowledge_store.update_progress(MATHE, status="in_progress", text="Ohne Abschnitte.")
    assert "- Status: in_progress" in progress.content
    assert "Ohne Abschnitte." in progress.content
    assert "## Verlauf" in progress.content

    topic = knowledge_store.update_topic(MATHE, "Fourier", status="not_started")
    assert "- Status: not_started" in topic.content
    assert "Noch keine Inhalte" in topic.content


def test_note_and_section_limits_stop_unbounded_growth(knowledge_store, monkeypatch):
    knowledge_store.ensure_context_knowledge(MATHE)
    monkeypatch.setattr("organiser.services.knowledge.document.MAX_SECTION_ENTRIES", 2)
    monkeypatch.setattr("organiser.services.knowledge.updates.MAX_SECTION_ENTRIES", 2)

    knowledge_store.append_note(MATHE, "Eins")
    knowledge_store.append_note(MATHE, "Zwei")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(MATHE, "Drei")


def test_list_topics_is_empty_without_a_topics_directory(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    (knowledge_store.context_directory(MATHE) / "topics").rmdir()

    assert knowledge_store.list_topics(MATHE) == ()


def test_root_is_created_on_demand(tmp_path):
    missing = tmp_path / "noch-nicht-da"
    store = KnowledgeStore(missing)

    assert not missing.exists()
    store.ensure_context_knowledge(MATHE)
    assert missing.is_dir()

    store = KnowledgeStore(tmp_path / "a" / "b")
    assert store.root.name == "b"
    store.ensure_context_knowledge(MATHE)
    assert (tmp_path / "a" / "b" / "UNI").is_dir()


def test_root_that_becomes_a_file_is_rejected(knowledge_store):
    context = knowledge_store.ensure_context_knowledge(MATHE)
    for child in sorted(context.directory.rglob("*"), reverse=True):
        child.rmdir() if child.is_dir() else child.unlink()
    context.directory.rmdir()
    context.directory.write_text("kein Verzeichnis", encoding="utf-8")

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory(MATHE)


def test_write_guards_against_a_file_outside_the_root(knowledge_store, monkeypatch):
    knowledge_store.ensure_context_knowledge(MATHE)
    original = Path.is_symlink

    monkeypatch.setattr(
        Path, "is_symlink", lambda self: True if self.name == "notes.md" else original(self)
    )

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.append_note(MATHE, "Nicht schreibbar.")


def test_symlink_inside_the_root_pointing_inside_is_also_rejected(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    (knowledge_store.root / "alias").symlink_to(
        knowledge_store.root / "UNI", target_is_directory=True
    )

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.ensure_context_knowledge("alias/Mathe/Mathe 2")


def test_contexts_stay_separate(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE, title="Mathe 2")
    knowledge_store.ensure_context_knowledge(STATISTIK, title="Statistik")

    knowledge_store.append_note(MATHE, "Nur Mathe.")
    knowledge_store.create_topic(STATISTIK, "Regression", text="Nur Statistik.")

    mathe = knowledge_store.context_directory(MATHE)
    statistik = knowledge_store.context_directory(STATISTIK)
    assert "Nur Mathe." in (mathe / "notes.md").read_text(encoding="utf-8")
    assert "Nur Mathe." not in (statistik / "notes.md").read_text(encoding="utf-8")
    assert knowledge_store.list_topics(MATHE) == ()
    assert knowledge_store.list_topics(STATISTIK) == ("Regression",)


def test_written_files_are_private_and_readable_markdown(knowledge_store):
    context = knowledge_store.ensure_context_knowledge(MATHE)
    knowledge_store.append_note(MATHE, "Notiz")

    for path in (*context.files, context.directory / "notes.md"):
        assert path.read_text(encoding="utf-8").startswith("# ")
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(context.directory).st_mode & 0o777) == "0o700"
