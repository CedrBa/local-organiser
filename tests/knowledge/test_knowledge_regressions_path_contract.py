"""Path contract contracts for knowledge."""

from __future__ import annotations

import pytest
from organiser.services.knowledge import (
    KnowledgeContentError,
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


# --- 4. Pfadvertrag ----------------------------------------------------------


@pytest.mark.parametrize(
    "candidate",
    [
        "/etc/passwd",
        "//server/share",
        "C:/Users/x",
        "C:\\x",
        "c:x",
        "..\\..\\Windows",
        "../outside",
        "a/../b",
        "a/./b",
        "a//b",
        "a/b/",
        ".",
        "..",
        "",
        "   ",
        " a",
        "a ",
        "a/ b",
        "-rf",
        "a/\x00b",
        "a/\x01b",
        "x" * 513,
    ],
)
def test_non_canonical_paths_are_rejected(knowledge_store, candidate):
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory(candidate)


@pytest.mark.parametrize(
    "candidate", ["a", "a/b", "UNI/Mathe/Mathe 2", "Mathe 2", "Projekt/Teil 1"]
)
def test_canonical_relative_posix_paths_are_accepted(knowledge_store, candidate):
    assert knowledge_store.context_directory(candidate) == knowledge_store.root.joinpath(
        *candidate.split("/")
    )


def test_path_validation_has_no_filesystem_side_effects(knowledge_store, tmp_path):
    before = sorted(p.name for p in knowledge_root_entries(knowledge_store))

    for candidate in ("../escape", "/abs", "C:/x", "a//b", "a/./b"):
        with pytest.raises(UnsafeKnowledgePathError):
            knowledge_store.context_directory(candidate)

    assert sorted(p.name for p in knowledge_root_entries(knowledge_store)) == before
    assert not (tmp_path / "escape").exists()


def test_whitespace_only_difference_is_not_silently_normalised(knowledge_store):
    with pytest.raises(UnsafeKnowledgePathError):
        knowledge_store.context_directory(" Mathe 2 ")


# --- 5. Topic-Titel ----------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "../../../etc/passwd",
        "..",
        "../Fourier",
        "Fourier/Analyse",
        "Fourier\\Analyse",
        "Fourier\nAnalyse",
        "Fourier\x00Analyse",
        "Fourier\x1fAnalyse",
        " Fourier",
        "Fourier ",
    ],
)
def test_path_shaped_topic_titles_are_refused(knowledge_store, title):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    with pytest.raises(KnowledgeContentError):
        knowledge_store.create_topic(CONTEXT, title, text="Inhalt.")
    with pytest.raises(KnowledgeContentError):
        knowledge_store.update_topic(CONTEXT, title, text="Inhalt.")


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Fourier", "Fourier.md"),
        ("Fourier-Analyse & Transforms", "Fourier-Analyse-Transforms.md"),
        ("Laplace   Transformation", "Laplace-Transformation.md"),
        ("Übungsblätter", "Ubungsblatter.md"),
        ("partielle Differentialgleichungen", "partielle-Differentialgleichungen.md"),
    ],
)
def test_semantic_topic_titles_are_slugged_deterministically(knowledge_store, title, expected):
    knowledge_store.ensure_context_knowledge(CONTEXT)
    first = knowledge_store.create_topic(CONTEXT, title, text="Inhalt.")

    assert first.path.name == expected
    assert knowledge_store.topic_path(CONTEXT, title).name == expected
