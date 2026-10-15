"""The bounded knowledge reader: no side effects, no path escapes, honest hashes.

These tests pin the security boundary the workspace read model depends on. If any of
them fails, the builder can no longer claim that it never writes, never creates and
never follows a link.
"""

from __future__ import annotations

import os
import stat

import pytest

from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
    KnowledgeError,
    KnowledgeExcerptTooLargeError,
    KnowledgeNotFoundError,
    KnowledgeStore,
    KnowledgeUnstableError,
    UnsafeKnowledgePathError,
)
from support.workspace import store as store


CONTEXT = "contexts/c1"


def test_read_excerpt_never_creates_a_missing_root(tmp_path):
    root = tmp_path / "does-not-exist"
    store = KnowledgeStore(root)

    with pytest.raises(KnowledgeNotFoundError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)

    assert not root.exists(), "ein Read darf den Knowledge Root nicht anlegen"


def test_read_excerpt_never_creates_a_missing_context_directory(tmp_path):
    store = KnowledgeStore(tmp_path)

    with pytest.raises(KnowledgeNotFoundError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)

    assert list(tmp_path.iterdir()) == [], "ein Read darf kein Verzeichnis anlegen"


def test_read_excerpt_returns_bounded_text_and_the_complete_file_hash(store):
    body = "Bilanzierung\n" * 5_000
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    (target / "overview.md").write_text(body, encoding="utf-8")

    read = store.read_excerpt(f"{CONTEXT}/overview.md", 100)

    assert read.truncated is True
    assert len(read.text) <= 100
    assert read.byte_size == len(body.encode("utf-8"))
    assert read.char_length == len(body)
    # The hash covers the *complete* file, not the excerpt: a later manifest check
    # can only work that way.
    import hashlib

    assert read.content_hash == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_read_excerpt_clamps_max_bytes_and_skips_oversized_files(store):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    (target / "overview.md").write_bytes(b"x" * 5_000)

    with pytest.raises(KnowledgeExcerptTooLargeError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100, 1_000)

    # Exactly at the requested limit is still readable.
    read = store.read_excerpt(f"{CONTEXT}/overview.md", 100, 5_000)
    assert read.byte_size == 5_000

    # A caller cannot raise the hard 256 KiB limit by asking for more.
    (target / "overview.md").write_bytes(b"x" * (MAX_EXCERPT_BYTES + 1))
    with pytest.raises(KnowledgeExcerptTooLargeError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100, MAX_EXCERPT_BYTES * 100)


def test_read_excerpt_refuses_a_symlinked_file(store, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("geheim", encoding="utf-8")
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    (target / "overview.md").symlink_to(outside)

    with pytest.raises(UnsafeKnowledgePathError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)


def test_read_excerpt_refuses_a_symlinked_intermediate_segment(store, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "overview.md").write_text("geheim", encoding="utf-8")
    (store.root / "contexts").mkdir()
    (store.root / CONTEXT).symlink_to(outside)

    with pytest.raises(UnsafeKnowledgePathError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)


@pytest.mark.parametrize("factory", [os.mkfifo])
def test_read_excerpt_refuses_special_files(store, factory):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    path = target / "overview.md"
    if hasattr(stat, "S_IFIFO"):
        factory(path)
    else:  # pragma: no cover - platform without fifos
        pytest.skip("no fifo support")

    with pytest.raises(UnsafeKnowledgePathError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)


def test_read_excerpt_refuses_a_directory(store):
    (store.root / CONTEXT).mkdir(parents=True)
    (store.root / CONTEXT / "overview.md").mkdir()

    with pytest.raises(UnsafeKnowledgePathError):
        store.read_excerpt(f"{CONTEXT}/overview.md", 100)


def test_read_excerpt_refuses_traversal_and_absolute_paths(store):
    for candidate in ("../etc/passwd", "/etc/passwd", f"{CONTEXT}/../../secrets.md"):
        with pytest.raises((UnsafeKnowledgePathError, KnowledgeError)):
            store.read_excerpt(candidate, 100)


def test_read_excerpt_refuses_non_markdown(store):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    (target / "notes.txt").write_text("x", encoding="utf-8")

    with pytest.raises(UnsafeKnowledgePathError):
        store.read_excerpt(f"{CONTEXT}/notes.txt", 100)


def test_read_excerpt_rejects_nonsense_limits(store):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    (target / "overview.md").write_text("x", encoding="utf-8")

    for value in (0, -1, "10", True, None):
        with pytest.raises(KnowledgeError):
            store.read_excerpt(f"{CONTEXT}/overview.md", value)


def test_read_excerpt_refuses_a_file_that_changes_while_being_read(store, monkeypatch):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    path = target / "overview.md"
    path.write_text("a" * 1_000, encoding="utf-8")
    store_ = store
    real_fstat = os.fstat

    def rewriting_fstat(descriptor):
        result = real_fstat(descriptor)
        # Simulate a concurrent write between the size check and the read.
        path.write_text("b" * 1_000, encoding="utf-8")
        os.utime(path, ns=(0, 0))
        return result

    monkeypatch.setattr(os, "fstat", rewriting_fstat)
    with pytest.raises(KnowledgeUnstableError):
        store_.read_excerpt(f"{CONTEXT}/overview.md", 100)


def test_read_excerpt_does_not_modify_the_file(store):
    target = store.root / CONTEXT
    target.mkdir(parents=True)
    path = target / "overview.md"
    path.write_text("Bilanzierung", encoding="utf-8")
    before = (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())

    store.read_excerpt(f"{CONTEXT}/overview.md", 4_000)

    after = (path.stat().st_mtime_ns, path.stat().st_size, path.read_bytes())
    assert before == after


def test_list_topic_files_is_read_only_sorted_and_bounded(store):
    target = store.root / CONTEXT / "topics"
    target.mkdir(parents=True)
    for name in (
        "zeta",
        "alpha",
        "mit leerzeichen",
        "topic-" + "0" * 8 + "-0000-0000-0000-000000000000",
    ):
        (target / f"{name}.md").write_text("x", encoding="utf-8")
    (target / "kein-markdown.txt").write_text("x", encoding="utf-8")
    (target / "verweis.md").symlink_to(target / "alpha.md")

    stems = store.list_topic_files(CONTEXT)

    assert "verweis" not in stems, "ein Symlink wird uebersprungen, nicht verfolgt"
    assert "kein-markdown" not in stems
    assert stems == tuple(sorted(stems))
    assert stems[:2] == ("alpha", "mit leerzeichen")
    assert len(store.list_topic_files(CONTEXT, limit=2)) == 2


def test_list_topic_files_creates_nothing_for_a_missing_root(tmp_path):
    root = tmp_path / "missing"
    store = KnowledgeStore(root)

    assert store.list_topic_files(CONTEXT) == ()
    assert not root.exists()


def test_existing_writer_behaviour_is_unchanged(knowledge_store):
    """The bounded reader is additive: create/update/read still behave as before."""
    knowledge_store.ensure_context_knowledge(CONTEXT, title="Finanzierung")
    knowledge_store.update_overview(CONTEXT, "Rechnungswesen")
    knowledge_store.create_topic(CONTEXT, "Bilanzierung", text="Aktiva")

    assert "Rechnungswesen" in knowledge_store.read(f"{CONTEXT}/overview.md")
    assert knowledge_store.list_topics(CONTEXT) == ("Bilanzierung",)
    assert (knowledge_store.root / CONTEXT / "topics" / "Bilanzierung.md").exists()
