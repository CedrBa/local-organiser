"""Enumeration safety contracts for ingestion."""

from __future__ import annotations

import os
import pytest
from sqlalchemy import select
from organiser.db.models import Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    ResourceIngestionError,
    SourceUnsafeError,
)
from organiser.services import source_scanner
from organiser.services.resources import scan_source
from support.ingestion import (
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)


# -- enumeration safety ------------------------------------------------------


def test_scan_registers_files_and_ignores_links_and_special_files(tmp_path):
    root = tmp_path / "kurs"
    (root / "kapitel").mkdir(parents=True)
    (root / "Vorlesung.pdf").write_bytes(b"%PDF-1.4 fake")
    (root / "kapitel" / "Uebung.md").write_text("uebung", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("intern", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "modul.js").write_text("js", encoding="utf-8")
    os.symlink("/etc/passwd", root / "Verweis.md")
    os.symlink("/etc", root / "kapitel" / "Verzeichnis")
    os.mkfifo(root / "pipe")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(session, source_id, settings)
        assert result.complete is True
        assert result.counters.created == 2
        assert result.counters.skipped_excluded == 2
        assert result.counters.skipped_unsafe == 3
        registered = sorted(
            resource.relative_path for resource in session.scalars(select(Resource))
        )
        assert registered == ["Vorlesung.pdf", "kapitel/Uebung.md"]
        assert (
            resource_by_locator(session, context_id=context_id, relative_path="Vorlesung.pdf")
            is not None
        )


def test_symlinked_source_directory_is_refused(tmp_path):
    real = tmp_path / "wirklich"
    real.mkdir()
    (real / "Notiz.md").write_text("x", encoding="utf-8")
    link = tmp_path / "verweis"
    link.symlink_to(real, target_is_directory=True)
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(link))
        with pytest.raises(SourceUnsafeError) as error:
            scan_source(session, source_id, settings)
        assert error.value.context["reason"] == "symlink"
        assert list(session.scalars(select(Resource))) == []


def test_intermediate_symlink_is_not_followed(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (tmp_path / "ausserhalb").mkdir()
    (tmp_path / "ausserhalb" / "geheim.md").write_text("geheim", encoding="utf-8")
    (root / "link").symlink_to(tmp_path / "ausserhalb", target_is_directory=True)
    settings = make_settings(tmp_path, allowed_roots=(root,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(session, source_id, settings)
        assert result.counters.created == 0
        assert result.counters.skipped_unsafe == 1
        policy = source_scanner.IngestionPolicy.from_settings(settings)
        with pytest.raises(ResourceIngestionError) as error:
            source_scanner.read_local_file(str(root / "link" / "geheim.md"), policy)
        assert error.value.code == "source_unsafe"


def test_special_files_are_never_read(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    os.mkfifo(root / "pipe")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    with pytest.raises(SourceUnsafeError) as error:
        source_scanner.read_local_file(str(root / "pipe"), policy)
    assert error.value.context["reason"] == "special_file"
    assert source_scanner.scan_source_tree(str(root), policy).files == ()


def test_file_changed_during_read_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("a" * 100, encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    real_read = os.read

    def swapping_read(fd, size):
        chunk = real_read(fd, size)
        if chunk:
            target.write_text("b" * 100, encoding="utf-8")
        return chunk

    monkeypatch.setattr(os, "read", swapping_read)
    with pytest.raises(ResourceIngestionError) as error:
        source_scanner.read_local_file(str(target), policy)
    assert error.value.code == "file_changed_during_read"


def test_replaced_file_is_rejected_after_read(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("original", encoding="utf-8")
    replacement = root / "neu.md"
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    real_fstat = os.fstat

    def moving_fstat(fd):
        status = real_fstat(fd)
        if not getattr(moving_fstat, "done", False):
            moving_fstat.done = True
            replacement.write_text("original", encoding="utf-8")
            os.replace(replacement, target)
        return status

    monkeypatch.setattr(os, "fstat", moving_fstat)
    with pytest.raises(ResourceIngestionError) as error:
        source_scanner.read_local_file(str(target), policy)
    assert error.value.code == "file_changed_during_read"


# -- limits ------------------------------------------------------------------


def test_entry_limit_makes_the_scan_incomplete(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(5):
        (root / f"Notiz{index}.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_entries=2)
        )
        assert result.complete is False
        assert result.status == "incomplete"
        assert result.error_code == "entry_limit_reached"
        assert result.counters.created == 2
        assert any(
            warning.code == "scan_incomplete_no_missing_decision" for warning in result.warnings
        )


def test_depth_limit_makes_the_scan_incomplete(tmp_path):
    root = tmp_path / "kurs"
    (root / "a" / "b" / "c").mkdir(parents=True)
    (root / "a" / "b" / "c" / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_depth=2)
        )
        assert result.complete is False
        assert result.error_code == "depth_limit_reached"
        assert result.counters.created == 0


def test_per_file_limit_leaves_the_resource_without_a_hash(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "gross.md").write_text("x" * 4096, encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_file_bytes=128)
        )
        assert result.complete is True
        assert result.counters.created == 1
        assert any(warning.code == "file_too_large" for warning in result.warnings)
        resource = resource_by_locator(session, context_id=context_id, relative_path="gross.md")
        assert resource.current_content_hash is None
        assert resource.byte_size == 4096
        assert resource.status == "active"
        assert snapshots_of(session, resource.id) == []


def test_scan_read_budget_makes_the_scan_incomplete(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(3):
        (root / f"Notiz{index}.md").write_text("x" * 100, encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_total_read_bytes=150)
        )
        assert result.complete is False
        assert result.error_code == "read_budget_exhausted"


def test_limits_can_only_be_lowered():
    narrowed = DEFAULT_LIMITS.narrowed(max_entries=10, max_pdf_pages=5)
    assert narrowed.max_entries == 10
    assert narrowed.max_pdf_pages == 5
    assert narrowed.max_text_characters == DEFAULT_LIMITS.max_text_characters
    assert DEFAULT_LIMITS.narrowed(max_entries=10**9).max_entries == DEFAULT_LIMITS.max_entries
    with pytest.raises(ValueError):
        DEFAULT_LIMITS.narrowed(max_entries=-1)
