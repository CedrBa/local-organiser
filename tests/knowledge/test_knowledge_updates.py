"""Updates contracts for knowledge."""

from __future__ import annotations

from datetime import UTC, datetime
import pytest
from organiser.core.config import Settings, get_settings
from organiser.services.knowledge import (
    KnowledgeContentError,
    get_knowledge_store,
)
from support.knowledge import (
    MATHE as MATHE,
    STATISTIK as STATISTIK,
    _write_core_file as _write_core_file,
)


def test_knowledge_root_is_configurable_and_never_inside_the_repository(monkeypatch, tmp_path):
    configured = tmp_path / "persoenliches-wissen"
    monkeypatch.setenv("ORGANISER_KNOWLEDGE_ROOT", str(configured))

    settings = get_settings()

    assert settings.knowledge_root == configured.resolve()
    assert get_knowledge_store(settings).root == configured.resolve()
    default_settings = Settings(database_path=tmp_path / "x.sqlite3")
    assert default_settings.knowledge_root.name == "knowledge"


def test_ensure_context_knowledge_creates_the_core_structure(knowledge_store, knowledge_root):
    context = knowledge_store.ensure_context_knowledge(MATHE, title="Mathe 2")

    assert context.created is True
    assert context.directory == knowledge_root / "UNI" / "Mathe" / "Mathe 2"
    for name in ("overview.md", "progress.md", "notes.md"):
        assert (context.directory / name).is_file()
    assert (context.directory / "topics").is_dir()
    assert (context.directory / "overview.md").read_text(encoding="utf-8").startswith("# Mathe 2\n")
    assert context.overview == context.directory / "overview.md"
    assert context.progress == context.directory / "progress.md"
    assert context.notes == context.directory / "notes.md"
    assert context.topics_directory == context.directory / "topics"


def test_ensure_context_knowledge_keeps_existing_files(knowledge_store):
    first = knowledge_store.ensure_context_knowledge(MATHE)
    knowledge_store.update_overview(MATHE, "Lineare Algebra ist abgeschlossen.")
    overview_before = (first.directory / "overview.md").read_text(encoding="utf-8")

    second = knowledge_store.ensure_context_knowledge(MATHE)

    assert second.created is False
    assert second.created_files == ()
    assert (second.directory / "overview.md").read_text(encoding="utf-8") == overview_before


def test_update_overview_rewrites_only_the_targeted_section(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE, title="Mathe 2")
    knowledge_store.update_overview(MATHE, "Analysis und Lineare Algebra")

    write = knowledge_store.update_overview(MATHE, "Fourier wird aktuell bearbeitet.")

    content = write.content
    assert write.changed is True
    assert "## Ueberblick" in content
    assert "Fourier wird aktuell bearbeitet." in content
    assert "Analysis und Lineare Algebra" not in content


def test_update_progress_keeps_status_text_and_history(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    moment = datetime(2026, 9, 29, 14, 30, tzinfo=UTC)

    knowledge_store.update_progress(
        MATHE, text="Kapitel 2 gelesen", status="in_progress", recorded_at=moment
    )
    write = knowledge_store.update_progress(MATHE, status="done", percent=100, recorded_at=moment)

    content = write.content
    assert "- Status: done" in content
    assert "- Fortschritt: 100 %" in content
    assert "Kapitel 2 gelesen" in content
    assert "## Verlauf" in content
    assert content.count("## Verlauf") == 1
    assert content.count("Kapitel 2 gelesen") == 2  # einmal als Stand, einmal im Verlauf
    assert content.count("- 2026-09-29 14:30 UTC Status: in_progress") == 1
    assert content.count("- 2026-09-29 14:30 UTC Status: done") == 1
    assert write.path.name == "progress.md"


def test_update_progress_rejects_empty_updates_and_invalid_percent(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_progress(MATHE)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_progress(MATHE, percent=140)


def test_update_progress_is_idempotent_for_identical_input(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    moment = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)

    knowledge_store.update_progress(MATHE, text="Kurz notiert", recorded_at=moment)
    second = knowledge_store.update_progress(MATHE, text="Kurz notiert", recorded_at=moment)

    assert second.changed is False
    assert second.content.count("## Verlauf") == 1
    assert second.content.count("Kurz notiert") == 2  # einmal als Stand, einmal als Verlaufseintrag


def test_append_note_appends_without_rewriting_earlier_notes(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    first = knowledge_store.append_note(
        MATHE,
        "Definition einer Fourier-Reihe.",
        heading="Fourier-Reihe",
        recorded_at=datetime(2026, 9, 1, 9, tzinfo=UTC),
    )
    write = knowledge_store.append_note(
        MATHE,
        "Zusaetzlich: Parseval.",
        heading="Fourier-Reihe",
        recorded_at=datetime(2026, 9, 2, 9, tzinfo=UTC),
    )

    assert "Definition einer Fourier-Reihe." in write.content
    assert "Zusaetzlich: Parseval." in write.content
    assert write.content.index("Definition einer Fourier-Reihe.") < write.content.index(
        "Zusaetzlich: Parseval."
    )
    assert first.changed is True


def test_append_note_uses_a_dated_heading_by_default(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    write = knowledge_store.append_note(MATHE, "Notiz ohne Titel")

    assert write.content.startswith("# Mathe 2")
    assert "## 20" in write.content


def test_topic_file_can_be_created_and_updated(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    created = knowledge_store.create_topic(
        MATHE, "Fourier", text="Grundlagen der Fourier-Reihe.", status="not_started"
    )
    updated = knowledge_store.update_topic(
        MATHE, "Fourier", text="Laplace-Transform ergaenzt.", status="in_progress", percent=40
    )

    assert created.path.name == "Fourier.md"
    assert created.path.parent.name == "topics"
    assert "- Status: in_progress" in updated.content
    assert "- Fortschritt: 40 %" in updated.content
    assert "Grundlagen der Fourier-Reihe." in updated.content
    assert "Laplace-Transform ergaenzt." in updated.content
    assert knowledge_store.list_topics(MATHE) == ("Fourier",)


def test_create_topic_refuses_to_overwrite_and_update_topic_can_append(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)
    knowledge_store.create_topic(MATHE, "Laplace", text="Grundlagen.")

    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(MATHE, "Laplace", text="Ueberschreiben?")

    appended = knowledge_store.update_topic(MATHE, "Laplace", text="Anwendung.")
    replaced = knowledge_store.update_topic(
        MATHE, "Laplace", text="Nur noch Anwendung.", append=False
    )

    assert appended.content.count("Grundlagen.") == 1
    assert "Anwendung." in appended.content
    assert "Grundlagen." not in replaced.content
    assert "Nur noch Anwendung." in replaced.content
