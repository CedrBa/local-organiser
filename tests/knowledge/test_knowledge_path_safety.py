"""Path safety contracts for knowledge."""

from __future__ import annotations

import pytest
from organiser.services.knowledge import (
    KnowledgeContentError,
    UnsafeKnowledgePathError,
)
from support.knowledge import (
    MATHE as MATHE,
    STATISTIK as STATISTIK,
    _write_core_file as _write_core_file,
)


def test_path_shaped_topic_titles_are_rejected_not_silently_renamed(
    knowledge_store, knowledge_root
):
    knowledge_store.ensure_context_knowledge(MATHE)
    topics = knowledge_root / "UNI" / "Mathe" / "Mathe 2" / "topics"

    for candidate in (
        "../../../etc/passwd",
        "..",
        "Fourier/Analyse",
        "Fourier\\Analyse",
        "Fourier\nAnalyse",
        "Fourier\x00Analyse",
        "../Fourier",
        " Fourier",
    ):
        with pytest.raises(KnowledgeContentError):
            knowledge_store.create_topic(MATHE, candidate, text="Nicht ausserhalb.")

    assert list(topics.iterdir()) == []


def test_normal_topic_titles_are_still_slugged_deterministically(knowledge_store):
    knowledge_store.ensure_context_knowledge(MATHE)

    first = knowledge_store.create_topic(MATHE, "Fourier-Analyse & Transforms", text="Inhalt.")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(MATHE, "Fourier-Analyse & Transforms", text="Doppelt.")
    unicode_topic = knowledge_store.create_topic(MATHE, "Übungsblätter", text="Inhalt.")

    # Umlaute werden per NFKD zerlegt; das ist deterministisch und dokumentiert.
    assert unicode_topic.path.name == "Ubungsblatter.md"
    assert first.path.name == "Fourier-Analyse-Transforms.md"
    assert knowledge_store.topic_path(MATHE, "Fourier-Analyse & Transforms") == first.path


def test_context_path_traversal_is_rejected(knowledge_store):
    for candidate in (
        "../../etc/passwd",
        "UNI/../../outside",
        "/etc/passwd",
        "UNI/./Mathe",
        "..",
        "   ",
        "UNI/\x00evil",
    ):
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.ensure_context_knowledge(candidate)
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.update_overview(candidate, "Test")
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.append_note(candidate, "Test")
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.context_directory(candidate)
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.list_topics(candidate)


def test_additional_path_edge_cases_are_rejected(knowledge_store):
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("x" * 600)
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("UNI//")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("--force")
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("UNI/..")


def test_backslash_paths_are_treated_as_traversal_attempts(knowledge_store):
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory("..\\..\\Windows")


def test_symlinked_context_path_is_rejected(knowledge_store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "context").mkdir()
    (knowledge_store.root / "link").symlink_to(outside / "context", target_is_directory=True)

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.ensure_context_knowledge("link")


def test_symlinked_file_inside_root_cannot_be_used_for_writes(knowledge_store, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("geheim", encoding="utf-8")
    context = knowledge_store.ensure_context_knowledge(MATHE)
    (context.directory / "notes.md").unlink()
    (context.directory / "notes.md").symlink_to(secret)

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.append_note(MATHE, "ueberschreiben?")

    assert secret.read_text(encoding="utf-8") == "geheim"


def test_symlinked_topics_directory_is_rejected(knowledge_store, tmp_path):
    outside = tmp_path / "outside-topics"
    outside.mkdir()
    context = knowledge_store.ensure_context_knowledge(MATHE)
    (context.directory / "topics").rmdir()
    (context.directory / "topics").symlink_to(outside, target_is_directory=True)

    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.ensure_context_knowledge(MATHE)

    assert list(outside.iterdir()) == []
