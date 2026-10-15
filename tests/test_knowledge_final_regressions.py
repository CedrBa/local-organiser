"""Regressionstests fuer die Review-Funde der finalen Welle am Knowledge Store.

Die Tests decken zwei verifizierte Defekte ab (mehrabsaetzige unmittelbare
Wiederholung, Replace als falscher No-op) und die daraus folgende bounded
Zusatzpruefung: Inhalt, der die Dokumentstruktur umdefinieren wuerde, wird vor
dem Schreiben kontrolliert abgelehnt. Zusaetzlich wird die Symlink-Policy von
``list_topics`` an die von ``read``/Schreiben angeglichen.
"""

import os
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from organiser.services.knowledge import (
    KnowledgeContentError,
    KnowledgeStore,
    UnsafeKnowledgePathError,
)


CONTEXT = "ctx"
TOPIC = "Fourier"
T1 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)

#: Ein mehrabsaetziger Block mit Unterpunkt und Codeblock; allesamt gueltiger Inhalt.
RICH_BLOCK = "Zeile 1\nZeile 2\n\nAbsatz 2\n\n### Unterpunkt\n\n```python\nprint('x')\n```\n\nEnde"


def _mtime(path: Path) -> int:
    return path.stat().st_mtime_ns


def _repeat_after_a_moment(knowledge_store: KnowledgeStore, call, path: Path):
    """Run ``call`` again after the filesystem timestamp would have moved on."""
    time.sleep(0.01)
    return call(), _mtime(path)


# --- 1. Unmittelbare Wiederholung eines mehrabsätzigen Appends ----------------


def test_repeated_multi_paragraph_topic_append_does_not_duplicate(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")
    first = knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, TOPIC)
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert path.read_text(encoding="utf-8") == first.content
    assert mtime_after == mtime
    assert repeat.content.count("B\n\nC") == 1
    assert "A" in repeat.content


def test_multi_paragraph_repeat_is_detected_on_the_whole_block_not_the_last_paragraph(
    knowledge_store,
):
    """Der Vergleich betrifft den gesamten Append-Block, nicht nur dessen letzten Absatz."""
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A\n\nC")

    write = knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1)

    assert write.changed is True
    assert "A\n\nC" in write.content
    assert write.content.count("B\n\nC") == 1
    assert write.content.rstrip().endswith("B\n\nC")


def test_repeated_multi_paragraph_append_with_extra_blank_lines(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")
    first = knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\n\nC\n\n", recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, TOPIC)
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\n\nC\n\n", recorded_at=T1),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert mtime_after == mtime
    assert repeat.content.count("B\n\n\nC") == 1


def test_repeated_rich_block_append_including_subheading_and_code_fence(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")
    first = knowledge_store.update_topic(CONTEXT, TOPIC, text=RICH_BLOCK, recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, TOPIC)
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_topic(CONTEXT, TOPIC, text=RICH_BLOCK, recorded_at=T1),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert mtime_after == mtime
    assert repeat.content.count("### Unterpunkt") == 1
    assert repeat.content.count("print('x')") == 1


def test_three_identical_appends_in_a_row_write_only_once(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")

    for expected in (True, False, False):
        write = knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1)
        assert write.changed is expected

    assert write.content.count("B\n\nC") == 1


def test_distinct_blocks_after_a_multi_paragraph_append_still_accumulate(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")
    knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1)

    write = knowledge_store.update_topic(CONTEXT, TOPIC, text="D", recorded_at=T1)

    assert write.changed is True
    assert write.content.index("B\n\nC") < write.content.index("\nD\n")
    assert write.content.count("B\n\nC") == 1


# --- 2. Replace-Semantik ------------------------------------------------------


def test_replace_with_the_last_paragraph_really_replaces_the_section(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="Erster Abschnitt\n\nB")

    write = knowledge_store.update_topic(CONTEXT, TOPIC, text="B", append=False, recorded_at=T1)

    assert write.changed is True
    assert "Erster Abschnitt" not in write.content
    assert "## Inhalt\n\nB\n" in write.content
    assert write.content.count("\nB\n") == 1


def test_replace_is_a_no_op_only_for_the_complete_target_section(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.create_topic(CONTEXT, TOPIC, text="A\n\nB")
    path = knowledge_store.topic_path(CONTEXT, TOPIC)
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_topic(
            CONTEXT, TOPIC, text="A\n\nB", append=False, recorded_at=T1
        ),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert mtime_after == mtime


def test_replace_keeps_managed_status_and_document_title(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A", status="in_progress")

    write = knowledge_store.update_topic(
        CONTEXT, TOPIC, text="B\n\nC", append=False, status="done", recorded_at=T1
    )

    assert write.content.startswith(f"# {TOPIC}\n")
    assert "- Status: done" in write.content
    assert "## Inhalt\n\nB\n\nC\n" in write.content


def test_append_of_text_that_is_already_the_whole_content_stays_a_no_op(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")

    write = knowledge_store.update_topic(CONTEXT, TOPIC, text="A", recorded_at=T1)

    assert write.changed is False
    assert write.content.count("\nA\n") == 1


def test_replace_followed_by_the_same_append_does_not_duplicate(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")
    knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", append=False, recorded_at=T1)
    path = knowledge_store.topic_path(CONTEXT, TOPIC)
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_topic(CONTEXT, TOPIC, text="B\n\nC", recorded_at=T1),
        path,
    )

    assert repeat.changed is False
    assert mtime_after == mtime
    assert repeat.content.count("B\n\nC") == 1


# --- 3. Mehrdeutige Struktur wird vor dem Schreiben abgelehnt -------------------

WRONG_HEADING = "Zeile 1\n\n## H2 im Text\n\nnach H2"
UNCLOSED_FENCE = "```python\nprint(1)\n"
CLOSED_FENCE = "```markdown\n## kein Abschnitt\n```\n\nDanach"


def _unreadable(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("payload", [WRONG_HEADING, UNCLOSED_FENCE])
def test_structurally_ambiguous_text_is_refused_by_every_text_entry_point(knowledge_store, payload):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="Inhalt.")
    directory = knowledge_store.context_directory(CONTEXT)
    topic = knowledge_store.topic_path(CONTEXT, TOPIC)
    before = {
        name: _unreadable(directory, name) for name in ("overview.md", "progress.md", "notes.md")
    }
    before[f"{TOPIC}.md"] = topic.read_text(encoding="utf-8")

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_overview(CONTEXT, payload)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_progress(CONTEXT, text=payload, status="in_progress", recorded_at=T1)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(CONTEXT, payload)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_topic(CONTEXT, TOPIC, text=payload, recorded_at=T1)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_topic(CONTEXT, TOPIC, text=payload, append=False, recorded_at=T1)

    after = {
        name: _unreadable(directory, name) for name in ("overview.md", "progress.md", "notes.md")
    }
    after[f"{TOPIC}.md"] = topic.read_text(encoding="utf-8")
    assert after == before


def test_ambiguous_text_does_not_create_a_topic_file(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)

    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(CONTEXT, "Neu", text=WRONG_HEADING)

    assert knowledge_store.list_topics(CONTEXT) == ()
    assert list(knowledge_store.root.rglob("*.tmp")) == []


def test_ambiguous_text_leaves_no_section_behind_in_the_managed_files(knowledge_store):
    """Der Abschnittsname wird nicht ins Dokument geschrieben, wenn der Text abgelehnt wird."""
    knowledge_store.ensure_context_knowledge(CONTEXT)
    before = _unreadable(knowledge_store.context_directory(CONTEXT), "overview.md")

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_overview(CONTEXT, "A\n\n## Verlauf\n\nB")

    after = _unreadable(knowledge_store.context_directory(CONTEXT), "overview.md")
    assert after == before
    assert after.count("## Verlauf") == 0


def test_heading_with_a_newline_is_refused_before_writing(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    directory = knowledge_store.context_directory(CONTEXT)
    overview_before = _unreadable(directory, "overview.md")
    notes_before = _unreadable(directory, "notes.md")

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_overview(CONTEXT, "Inhalt", heading="Ueberblick\n## Zweite")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.append_note(CONTEXT, "Notiz", heading="## Notiz")

    assert _unreadable(directory, "overview.md") == overview_before
    assert _unreadable(directory, "notes.md") == notes_before


def test_closed_code_fence_with_an_h2_line_is_valid_content(knowledge_store):
    """Eine H2-Zeile *im* Codeblock ist Inhalt, kein Abschnitt, und bleibt wiederholbar."""
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")

    first = knowledge_store.update_topic(CONTEXT, TOPIC, text=CLOSED_FENCE, recorded_at=T1)
    repeat = knowledge_store.update_topic(CONTEXT, TOPIC, text=CLOSED_FENCE, recorded_at=T1)
    overview = knowledge_store.update_overview(CONTEXT, CLOSED_FENCE)

    assert "## kein Abschnitt" in first.content
    assert repeat.changed is False
    assert repeat.content == first.content
    assert overview.changed is True
    assert knowledge_store.update_overview(CONTEXT, CLOSED_FENCE).changed is False


def test_h3_subheadings_are_not_treated_as_structure(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, TOPIC, text="A")

    write = knowledge_store.update_topic(
        CONTEXT, TOPIC, text="B\n\n### Unterpunkt\n\nC", recorded_at=T1
    )

    assert "### Unterpunkt" in write.content
    assert write.content.count("B") == 1


# --- 3b. Wiederholung in Overview, Progress und Notes -------------------------


def test_repeated_multi_paragraph_overview_update_is_idempotent(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.update_overview(CONTEXT, "Zeile 1\n\nZeile 2")
    path = knowledge_store.context_directory(CONTEXT) / "overview.md"
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_overview(CONTEXT, "Zeile 1\n\nZeile 2"),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert mtime_after == mtime


def test_repeated_multi_paragraph_progress_update_does_not_extend_the_history(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.update_progress(
        CONTEXT, text="Zeile 1\n\nZeile 2", status="in_progress", recorded_at=T1
    )
    path = knowledge_store.context_directory(CONTEXT) / "progress.md"
    mtime = _mtime(path)

    repeat, mtime_after = _repeat_after_a_moment(
        knowledge_store,
        lambda: knowledge_store.update_progress(
            CONTEXT, text="Zeile 1\n\nZeile 2", status="in_progress", recorded_at=T1
        ),
        path,
    )

    assert repeat.changed is False
    assert repeat.content == first.content
    assert mtime_after == mtime
    assert repeat.content.count("## Verlauf") == 1
    assert repeat.content.count("Zeile 1") == 2  # einmal als Stand, einmal im Verlauf


def test_repeated_multi_paragraph_note_stays_a_real_append(knowledge_store):
    """append_note bleibt bewusst nicht-idempotent; bestehende Notizen bleiben erhalten."""
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.append_note(
        CONTEXT, "Zeile 1\n\nZeile 2", heading="Notiz", recorded_at=T1
    )

    second = knowledge_store.append_note(
        CONTEXT, "Zeile 1\n\nZeile 2", heading="Notiz", recorded_at=T1
    )

    assert second.changed is True
    assert second.content.count("Zeile 1") == 2
    assert first.content in second.content
    assert second.content.count("## Notiz") == 2


# --- 4. list_topics prueft jeden Eintrag vor dem Zugriff ----------------------


def test_list_topics_refuses_a_symlinked_entry(knowledge_store, tmp_path):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, "Echt", text="Inhalt.")
    secret = tmp_path / "aussen.md"
    secret.write_text("aussen", encoding="utf-8")
    link = knowledge_store.context_directory(CONTEXT) / "topics" / "Fremd.md"
    link.symlink_to(secret)

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.list_topics(CONTEXT)

    assert link.is_symlink()
    assert secret.read_text(encoding="utf-8") == "aussen"


def test_list_topics_refuses_a_symlink_pointing_inside_the_root(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, "Echt", text="Inhalt.")
    target = knowledge_store.context_directory(CONTEXT) / "notes.md"
    (knowledge_store.context_directory(CONTEXT) / "topics" / "Alias.md").symlink_to(target)

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.list_topics(CONTEXT)


def test_list_topics_refuses_an_irregular_entry(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    os.mkfifo(knowledge_store.context_directory(CONTEXT) / "topics" / "Pipe.md")

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.list_topics(CONTEXT)


def test_list_topics_still_lists_regular_topics(knowledge_store):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    knowledge_store.create_topic(CONTEXT, "Zeta", text="Inhalt.")
    knowledge_store.create_topic(CONTEXT, "Alpha", text="Inhalt.")
    (knowledge_store.context_directory(CONTEXT) / "topics" / "notes.txt").write_text(
        "kein topic", encoding="utf-8"
    )

    assert knowledge_store.list_topics(CONTEXT) == ("Alpha", "Zeta")
