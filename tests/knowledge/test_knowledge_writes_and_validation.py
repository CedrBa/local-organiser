"""Writes and validation contracts for knowledge."""

from __future__ import annotations

import os
from datetime import datetime
import pytest
from pydantic import ValidationError
from organiser.domain.knowledge import KnowledgeUpdate
from organiser.services.knowledge import (
    KnowledgeContentError,
    KnowledgeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    UnsafeKnowledgePathError,
)
from support.knowledge import (
    MATHE as MATHE,
    STATISTIK as STATISTIK,
    _write_core_file as _write_core_file,
)


def test_interrupted_write_keeps_the_previous_file_intact(knowledge_store, monkeypatch):
    knowledge_store.ensure_context_knowledge(MATHE)
    knowledge_store.update_overview(MATHE, "Stabiler Inhalt.")
    path = knowledge_store.context_directory(MATHE) / "overview.md"

    def explode(*args, **kwargs):
        raise KeyboardInterrupt("Schreibvorgang abgebrochen")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(KeyboardInterrupt):
        knowledge_store.update_overview(MATHE, "Neuer Inhalt.")

    assert "Stabiler Inhalt." in path.read_text(encoding="utf-8")
    assert list(path.parent.glob(".*tmp")) == []


def test_read_returns_markdown_only_inside_the_root(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    knowledge_store.append_note(MATHE, "Lesbare Notiz")

    assert "Lesbare Notiz" in knowledge_store.read(f"{MATHE}/notes.md")
    with pytest.raises(KnowledgeNotFoundError):
        knowledge_store.read(f"{MATHE}/fehlt.md")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.read("../notes.md")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.read(f"{MATHE}/notes.txt")


def test_store_rejects_a_root_that_is_a_file(tmp_path):
    target = tmp_path / "not-a-directory"
    target.write_text("x", encoding="utf-8")

    with pytest.raises(KnowledgeError):
        KnowledgeStore(target)


def test_validated_knowledge_update_is_the_only_way_in(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    write = knowledge_store.apply(
        KnowledgeUpdate(
            action="update_progress",
            context_path=MATHE,
            status="in_progress",
            percent=75,
            text="Fourier aktiv",
        )
    )
    note = knowledge_store.apply(
        KnowledgeUpdate(action="append_note", context_path=MATHE, text="Notiz aus dem Proposal")
    )
    overview = knowledge_store.apply(
        KnowledgeUpdate(
            action="update_overview",
            context_path=MATHE,
            text="Mathe 2 im Ueberblick",
            heading="Ueberblick",
        )
    )
    topic = knowledge_store.apply(
        KnowledgeUpdate(
            action="create_topic",
            context_path=MATHE,
            topic="Laplace",
            text="Grundlagen.",
            status="not_started",
        )
    )
    updated = knowledge_store.apply(
        KnowledgeUpdate(
            action="update_topic",
            context_path=MATHE,
            topic="Laplace",
            text="Anwendung ergaenzt.",
            status="in_progress",
        )
    )

    assert "- Fortschritt: 75 %" in write.content
    assert "Notiz aus dem Proposal" in note.content
    assert "Mathe 2 im Ueberblick" in overview.content
    assert "- Status: not_started" in topic.content
    assert "- Status: in_progress" in updated.content
    assert "Anwendung ergaenzt." in updated.content
    assert "Grundlagen." in updated.content


def test_knowledge_update_rejects_incomplete_proposals():
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="create_topic", context_path=MATHE)
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="update_progress", context_path=MATHE)
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="update_topic", context_path=MATHE, topic="Fourier")
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="append_note", context_path=MATHE, text="   ")
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="update_overview", context_path=MATHE, text="ok", percent=10)
    with pytest.raises(ValidationError):
        KnowledgeUpdate(
            action="update_progress", context_path=MATHE, status="in_progress", unknown_field="x"
        )


def test_content_limits_are_enforced(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(MATHE, "x" * 20_001)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(MATHE, "mit\x00Nullzeichen")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_overview(MATHE, "   ")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(MATHE, "   ")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_topic(MATHE, "Fourier", percent=101)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(MATHE, 42)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(MATHE, "   ", text="x")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(MATHE, "!!!", text="x")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_topic(MATHE, "Fourier")


def test_segments_starting_with_a_dash_are_rejected(knowledge_store):
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.ensure_context_knowledge("-rf")


def test_naive_timestamps_are_treated_as_utc(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    write = knowledge_store.update_progress(
        MATHE, status="in_progress", recorded_at=datetime(2026, 9, 29, 12, 0)
    )

    assert "2026-09-29 12:00 UTC" in write.content


def test_unicode_titles_become_readable_topic_files(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    write = knowledge_store.create_topic(MATHE, "Fourier-Analyse & Transforms", text="Inhalt.")

    assert write.path.name == "Fourier-Analyse-Transforms.md"


def test_knowledge_update_rejects_unknown_actions():
    with pytest.raises(ValidationError):
        KnowledgeUpdate(action="delete_everything", context_path=MATHE, text="x")
