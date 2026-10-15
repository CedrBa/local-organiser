"""Content preservation contracts for knowledge."""

from __future__ import annotations

import pytest
from organiser.services.knowledge import (
    KnowledgeContentError,
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


# --- 1. Datenverlust ---------------------------------------------------------


def test_free_text_between_title_and_first_section_survives(knowledge_store):
    write_core_file(
        knowledge_store,
        "progress.md",
        "# Fortschritt\n\nWichtige handschriftliche Einleitung.\n\n"
        "## Status\n\n- Status: unbekannt\n\n## Fortschritt\n\nalt\n",
    )

    write = knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)

    assert "Wichtige handschriftliche Einleitung." in write.content
    assert "alt" not in write.content
    assert "neu" in write.content


def test_document_without_any_h2_keeps_its_text(knowledge_store):
    write_core_file(
        knowledge_store, "progress.md", "# Fortschritt\n\nNur freier, wichtiger Text.\n"
    )

    write = knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)

    assert "Nur freier, wichtiger Text." in write.content
    assert "## Status" in write.content
    assert "neu" in write.content


def test_unmanaged_h2_sections_are_never_dropped(knowledge_store):
    write_core_file(
        knowledge_store,
        "progress.md",
        "# Fortschritt\n\n## Status\n\n- Status: unbekannt\n\n## Fortschritt\n\nalt\n\n"
        "## Pruefungen\n\n3 von 10 Uebungen bestanden.\n\n## Quellen\n\nSkript Seite 12.\n",
    )

    write = knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)

    assert "## Pruefungen" in write.content
    assert "3 von 10 Uebungen bestanden." in write.content
    assert "## Quellen" in write.content
    assert "Skript Seite 12." in write.content


def test_h2_inside_a_fenced_code_block_is_treated_as_content(knowledge_store):
    write_core_file(
        knowledge_store,
        "progress.md",
        "# Fortschritt\n\n## Status\n\n- Status: unbekannt\n\n## Fortschritt\n\nalt\n\n"
        "## Beispiele\n\n```markdown\n## Kein echter Abschnitt\n- Status: manipuliert\n```\n\nNach dem Block.\n",
    )

    write = knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)

    assert "## Beispiele" in write.content
    assert "## Kein echter Abschnitt" in write.content
    assert "- Status: manipuliert" in write.content
    assert "Nach dem Block." in write.content
    # Der manipulierte Status innerhalb des Blocks darf den echten Status nicht ersetzen.
    assert "- Status: unbekannt" in write.content


def test_allowed_markdown_survives_repeated_updates(knowledge_store):
    write_core_file(
        knowledge_store,
        "progress.md",
        "# Fortschritt\n\nEinleitung bleibt.\n\n## Notizen\n\n- [ ] Uebung 1\n- [ ] Uebung 2\n",
    )

    first = knowledge_store.update_progress(
        CONTEXT, text="Runde 1", status="in_progress", recorded_at=T1
    )
    second = knowledge_store.update_progress(CONTEXT, status="done", percent=50, recorded_at=T2)
    third = knowledge_store.update_progress(CONTEXT, text="Runde 2", recorded_at=T2)

    for write in (first, second, third):
        assert "Einleitung bleibt." in write.content
        assert "## Notizen" in write.content
        assert "- [ ] Uebung 1" in write.content
        assert "- [ ] Uebung 2" in write.content
    assert "Runde 1" in third.content


def test_append_note_preserves_existing_introduction_and_notes(knowledge_store):
    write_core_file(
        knowledge_store,
        "notes.md",
        "# Mathe 2\n\nEinleitung nach der Ueberschrift.\n\n## Fourier-Reihe\n\nErster Teil.\n",
    )

    write = knowledge_store.append_note(
        CONTEXT, "Zweiter Teil.", heading="Fourier-Reihe", recorded_at=T1
    )

    assert "Einleitung nach der Ueberschrift." in write.content
    assert "Erster Teil." in write.content
    assert "Zweiter Teil." in write.content
    assert write.content.index("Erster Teil.") < write.content.index("Zweiter Teil.")


def test_ambiguous_managed_sections_fail_instead_of_dropping_content(knowledge_store):
    write_core_file(
        knowledge_store,
        "progress.md",
        "# Fortschritt\n\n## Status\n\n- Status: unbekannt\n\n## status\n\n- Status: done\n",
    )
    before = (
        knowledge_store.context_directory(CONTEXT)
        .joinpath("progress.md")
        .read_text(encoding="utf-8")
    )

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_progress(CONTEXT, text="neu", recorded_at=T1)

    assert (
        knowledge_store.context_directory(CONTEXT)
        .joinpath("progress.md")
        .read_text(encoding="utf-8")
        == before
    )


def test_overview_keeps_unmanaged_sections(knowledge_store):
    write_core_file(
        knowledge_store,
        "overview.md",
        "# Mathe 2\n\nEinleitung.\n\n## Ueberblick\n\nalt\n\n## Extras\n\n- Eigenes Feld\n",
    )

    write = knowledge_store.update_overview(CONTEXT, "neuer Ueberblick")

    assert "Einleitung." in write.content
    assert "## Extras" in write.content
    assert "- Eigenes Feld" in write.content
    assert "neuer Ueberblick" in write.content
    assert "alt" not in write.content
