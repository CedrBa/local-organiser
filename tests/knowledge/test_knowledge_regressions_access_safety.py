"""Access safety contracts for knowledge."""

from __future__ import annotations

import os
import pytest
from organiser.services.knowledge import (
    UnsafeKnowledgePathError,
)
from support.knowledge_regressions import (
    CONTEXT as CONTEXT,
    T1 as T1,
    T2 as T2,
    write_core_file as write_core_file,
    AccessRecorder as AccessRecorder,
    ORIGINAL_READ_TEXT as ORIGINAL_READ_TEXT,
    knowledge_root_entries as knowledge_root_entries,
)


def test_symlinked_core_file_is_rejected_before_any_read(knowledge_store, tmp_path, monkeypatch):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    secret = tmp_path / "secret.md"
    secret.write_text("GEHEIM", encoding="utf-8")
    context = knowledge_store.context_directory(CONTEXT)
    (context / "notes.md").unlink()
    (context / "notes.md").symlink_to(secret)

    recorder = AccessRecorder(monkeypatch)
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.append_note(CONTEXT, "Nicht schreibbar.")

    assert recorder.reads == []
    assert ORIGINAL_READ_TEXT(secret, encoding="utf-8") == "GEHEIM"


def test_symlinked_core_file_is_rejected_before_any_read_via_read(
    knowledge_store, tmp_path, monkeypatch
):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    secret = tmp_path / "secret.md"
    secret.write_text("GEHEIM", encoding="utf-8")
    context = knowledge_store.context_directory(CONTEXT)
    (context / "overview.md").unlink()
    (context / "overview.md").symlink_to(secret)

    recorder = AccessRecorder(monkeypatch)
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.update_overview(CONTEXT, "Nicht lesbar.")

    assert recorder.reads == []


def test_symlinked_topics_directory_is_rejected_before_any_listing(
    knowledge_store, tmp_path, monkeypatch
):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    outside = tmp_path / "outside-topics"
    outside.mkdir()
    (outside / "Fremd.md").write_text("fremd", encoding="utf-8")
    context = knowledge_store.context_directory(CONTEXT)
    (context / "topics").rmdir()
    (context / "topics").symlink_to(outside, target_is_directory=True)

    recorder = AccessRecorder(monkeypatch)
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.list_topics(CONTEXT)

    assert recorder.scans == []


def test_symlinked_topics_directory_is_rejected_when_ensuring(
    knowledge_store, tmp_path, monkeypatch
):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    outside = tmp_path / "outside-topics"
    outside.mkdir()
    context = knowledge_store.context_directory(CONTEXT)
    (context / "topics").rmdir()
    (context / "topics").symlink_to(outside, target_is_directory=True)

    recorder = AccessRecorder(monkeypatch)
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.ensure_context_knowledge(CONTEXT)

    assert recorder.scans == []
    assert list(outside.iterdir()) == []


def test_symlink_inside_the_root_is_rejected_by_every_entry_point(knowledge_store, tmp_path):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    (knowledge_store.root / "alias").symlink_to(
        knowledge_store.root / "UNI", target_is_directory=True
    )

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("alias/Mathe/Mathe 2")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.list_topics("alias/Mathe/Mathe 2")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.append_note("alias/Mathe/Mathe 2", "x")


def test_non_regular_files_are_refused_without_blocking(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    context = knowledge_store.context_directory(CONTEXT)
    (context / "notes.md").unlink()
    os.mkfifo(context / "notes.md")

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.append_note(CONTEXT, "x")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.read(f"{CONTEXT}/notes.md")


def test_read_refuses_a_non_regular_file(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    context = knowledge_store.context_directory(CONTEXT)
    (context / "progress.md").unlink()
    os.mkfifo(context / "progress.md")

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.read(f"{CONTEXT}/progress.md")


def test_interrupted_write_keeps_previous_content_and_cleans_up(knowledge_store, monkeypatch):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_overview(CONTEXT, "Stabiler Inhalt.")
    path = knowledge_store.context_directory(CONTEXT) / "overview.md"
    before = path.read_text(encoding="utf-8")

    def explode(*args, **kwargs):
        raise KeyboardInterrupt("Schreibvorgang abgebrochen")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(KeyboardInterrupt):
        knowledge_store.update_overview(CONTEXT, "Neuer Inhalt.")

    assert path.read_text(encoding="utf-8") == before
    assert list(path.parent.glob(".*tmp")) == []


def test_successful_write_leaves_no_temporary_file(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.append_note(CONTEXT, "Notiz")

    leftovers = [p.name for p in knowledge_store.root.rglob("*.tmp")]
    assert leftovers == []
