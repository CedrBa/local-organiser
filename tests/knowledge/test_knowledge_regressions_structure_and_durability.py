"""Structure and durability contracts for knowledge."""

from __future__ import annotations

import os
import time
import pytest
from organiser.services.knowledge import (
    KnowledgeContentError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
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


# --- 6. Kein stilles Verwerfen bei unveraenderter Struktur -------------------


def test_missing_file_is_created_without_touching_existing_ones(knowledge_store):
    context = knowledge_store.ensure_context_knowledge(CONTEXT, title="Mathe 2")
    knowledge_store.update_overview(CONTEXT, "Bestehender Inhalt.")
    overview_before = (context.directory / "overview.md").read_text(encoding="utf-8")

    again = knowledge_store.ensure_context_knowledge(CONTEXT)

    assert again.created_files == ()
    assert (again.directory / "overview.md").read_text(encoding="utf-8") == overview_before


def test_read_missing_file_reports_not_found(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)

    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read(f"{CONTEXT}/fehlt.md")


def test_operations_create_a_title_when_the_file_has_none(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    context = knowledge_store.context_directory(CONTEXT)
    for name in ("overview.md", "progress.md", "notes.md"):
        (context / name).unlink()
    (context / "overview.md").write_text("", encoding="utf-8")
    (context / "progress.md").write_text("", encoding="utf-8")
    (context / "notes.md").write_text("", encoding="utf-8")

    overview = knowledge_store.update_overview(CONTEXT, "Ohne Titel davor.")
    progress = knowledge_store.update_progress(CONTEXT, text="Ohne Titel", recorded_at=T1)
    note = knowledge_store.append_note(CONTEXT, "Ohne Titel")

    for write in (overview, progress, note):
        assert write.content.startswith("# ")


def test_empty_progress_file_gets_the_default_progress_text(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    (knowledge_store.context_directory(CONTEXT) / "progress.md").write_text("", encoding="utf-8")

    write = knowledge_store.update_progress(CONTEXT, status="in_progress", recorded_at=T1)

    assert "Noch kein Fortschritt dokumentiert." in write.content
    assert "- Status: in_progress" in write.content


def test_status_only_topic_update_on_an_existing_file_is_written(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, "Fourier", text="Inhalt.", status="not_started")

    write = knowledge_store.update_topic(CONTEXT, "Fourier", status="done", recorded_at=T2)

    assert write.changed is True
    assert "- Status: done" in write.content
    assert "Inhalt." in write.content


def test_topic_status_only_repeat_does_not_rewrite(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.update_topic(CONTEXT, "Fourier", status="in_progress", recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, "Fourier")
    mtime = path.stat().st_mtime_ns

    time.sleep(0.01)
    repeat = knowledge_store.update_topic(CONTEXT, "Fourier", status="in_progress", recorded_at=T2)

    assert repeat.changed is False
    assert repeat.content == first.content
    assert path.stat().st_mtime_ns == mtime


def test_document_with_too_many_sections_is_refused(knowledge_store, monkeypatch):
    monkeypatch.setattr("organiser.services.knowledge.document.MAX_SECTION_ENTRIES", 2)
    monkeypatch.setattr("organiser.services.knowledge.updates.MAX_SECTION_ENTRIES", 2)
    knowledge_store.ensure_context_knowledge(CONTEXT)
    write_core_file(
        knowledge_store,
        "overview.md",
        "# Mathe 2\n\n## A\n\n1\n\n## B\n\n2\n",
    )

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_overview(CONTEXT, "C")


def test_duplicate_heading_inside_with_body_of_is_reported(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    write_core_file(
        knowledge_store, "progress.md", "# Fortschritt\n\n## Verlauf\n\na\n\n## Verlauf\n\nb\n"
    )

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)


def test_root_that_is_not_a_directory_is_reported(knowledge_store, tmp_path):
    blocked = tmp_path / "blocked"
    blocked.write_text("kein Verzeichnis", encoding="utf-8")

    with pytest.raises(KnowledgeError):
        KnowledgeStore(blocked)


def test_file_and_directory_are_both_fsynced(knowledge_store, monkeypatch):
    """Atomicity rests on fsync of the file and of its directory."""
    knowledge_store.ensure_context_knowledge(CONTEXT)
    original_fsync = os.fsync
    seen: list[int] = []

    def counting_fsync(fd):
        seen.append(fd)
        return original_fsync(fd)

    monkeypatch.setattr(os, "fsync", counting_fsync)
    write = knowledge_store.append_note(CONTEXT, "Notiz mit fsync.")

    assert "Notiz mit fsync." in write.content
    assert len(seen) == 2
    assert list(knowledge_store.root.rglob("*.tmp")) == []
