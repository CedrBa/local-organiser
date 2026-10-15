"""Repeatability contracts for knowledge."""

from __future__ import annotations

import time
from support.knowledge_regressions import (
    CONTEXT as CONTEXT,
    T1 as T1,
    T2 as T2,
    write_core_file as write_core_file,
    AccessRecorder as AccessRecorder,
    ORIGINAL_READ_TEXT as ORIGINAL_READ_TEXT,
    knowledge_root_entries as knowledge_root_entries,
)


# --- 3. Wiederholungssemantik ------------------------------------------------


def test_repeating_the_same_progress_update_does_not_write_or_extend_history(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_progress(CONTEXT, text="A", status="in_progress", recorded_at=T1)
    second = knowledge_store.update_progress(
        CONTEXT, text="B", status="done", percent=100, recorded_at=T2
    )
    path = knowledge_store.context_directory(CONTEXT) / "progress.md"
    mtime_after_second = path.stat().st_mtime_ns

    time.sleep(0.01)
    third = knowledge_store.update_progress(
        CONTEXT, text="B", status="done", percent=100, recorded_at=T2
    )

    assert third.changed is False
    assert third.content == second.content
    assert path.read_text(encoding="utf-8") == second.content
    assert path.stat().st_mtime_ns == mtime_after_second
    assert second.content.count("## Verlauf") == 1
    assert second.content.count("- 2026-09-29 09:00 UTC") == 1


def test_repeating_progress_does_not_duplicate_after_several_history_entries(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    for index in range(4):
        knowledge_store.update_progress(CONTEXT, text=f"Schritt {index}", recorded_at=T1)
    last = knowledge_store.update_progress(CONTEXT, text="Fertig", status="done", recorded_at=T2)
    history_before = last.content.count("- 2026-09-29")

    repeat = knowledge_store.update_progress(CONTEXT, text="Fertig", status="done", recorded_at=T2)

    assert repeat.changed is False
    assert repeat.content == last.content
    assert repeat.content.count("- 2026-09-29") == history_before


def test_semantically_unchanged_status_does_not_write_only_for_the_timestamp(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_progress(
        CONTEXT, status="in_progress", percent=25, text="Stand", recorded_at=T1
    )
    path = knowledge_store.context_directory(CONTEXT) / "progress.md"
    mtime = path.stat().st_mtime_ns
    content_before = path.read_text(encoding="utf-8")

    time.sleep(0.01)
    repeat = knowledge_store.update_progress(
        CONTEXT, status="in_progress", percent=25, text="Stand", recorded_at=T2
    )

    assert repeat.changed is False
    assert path.stat().st_mtime_ns == mtime
    assert path.read_text(encoding="utf-8") == content_before
    assert "2026-09-29 09:00 UTC" not in content_before


def test_changed_status_still_writes(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_progress(CONTEXT, status="in_progress", recorded_at=T1)

    changed = knowledge_store.update_progress(CONTEXT, status="done", recorded_at=T2)

    assert changed.changed is True
    assert "- Status: done" in changed.content
    assert "2026-09-29 09:00 UTC" in changed.content


def test_repeating_the_same_topic_append_does_not_duplicate(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_topic(CONTEXT, "Fourier", text="A", recorded_at=T1)
    first = knowledge_store.update_topic(CONTEXT, "Fourier", text="B", recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, "Fourier")
    mtime = path.stat().st_mtime_ns

    time.sleep(0.01)
    second = knowledge_store.update_topic(CONTEXT, "Fourier", text="B", recorded_at=T1)

    assert second.changed is False
    assert second.content == first.content
    assert path.read_text(encoding="utf-8") == first.content
    assert path.stat().st_mtime_ns == mtime
    assert second.content.count("\nB\n") == 1


def test_distinct_topic_appends_still_accumulate(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.update_topic(CONTEXT, "Fourier", text="A", recorded_at=T1)
    knowledge_store.update_topic(CONTEXT, "Fourier", text="B", recorded_at=T1)

    third = knowledge_store.update_topic(CONTEXT, "Fourier", text="C", recorded_at=T1)

    assert third.changed is True
    assert "\nA\n" in third.content
    assert "\nB\n" in third.content
    assert "\nC\n" in third.content


def test_repeated_note_with_same_text_is_a_real_append(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.append_note(CONTEXT, "Gleiche Notiz.", heading="Notiz", recorded_at=T1)

    second = knowledge_store.append_note(CONTEXT, "Gleiche Notiz.", heading="Notiz", recorded_at=T1)

    assert second.changed is True
    assert second.content.count("Gleiche Notiz.") == 2
    assert first.content in second.content
