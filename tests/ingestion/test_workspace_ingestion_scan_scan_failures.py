"""Scan failures contracts for ingestion."""

from __future__ import annotations

import os
import pytest
from sqlalchemy import select
from organiser.db.models import ProcessingRun, Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    ResourceIngestionError,
    SourceUnsafeError,
)
from organiser.services import source_scanner
from organiser.services.resources import scan_source
from support.ingestion import (
    close_boundary,
    make_settings,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)


def test_symlinked_document_source_is_refused(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "echt.md").write_text("x", encoding="utf-8")
    (root / "verweis.md").symlink_to(root / "echt.md")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(
            session, context_id, path=str(root / "verweis.md"), source_type="note"
        )
        with pytest.raises(SourceUnsafeError) as error:
            scan_source(session, source_id, settings)
        assert error.value.context["reason"] == "symlink"


def test_unreadable_subdirectory_makes_the_scan_incomplete_and_marks_nothing(tmp_path):
    """A directory we may not enter may contain resources: absence is not proven."""
    root = tmp_path / "kurs"
    (root / "sichtbar").mkdir(parents=True)
    (root / "sichtbar" / "A.md").write_text("a", encoding="utf-8")
    hidden = root / "gesperrt"
    hidden.mkdir()
    (hidden / "B.md").write_text("b", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_ids = {
            resource.relative_path: resource.id for resource in session.scalars(select(Resource))
        }
        assert resource_ids == {
            "sichtbar/A.md": resource_ids["sichtbar/A.md"],
            "gesperrt/B.md": resource_ids["gesperrt/B.md"],
        }
        hidden_id = resource_ids["gesperrt/B.md"]
        close_boundary(session)
        hidden.chmod(0o000)
        try:
            result = scan_source(session, source_id, settings)
        finally:
            hidden.chmod(0o700)
        assert result.complete is False
        assert result.error_code == "directory_unreadable"
        assert result.counters.marked_missing == 0
        close_boundary(session)
        assert session.get(Resource, hidden_id).status == "active"


def test_file_changing_between_enumeration_and_extraction_does_not_duplicate_snapshots(
    tmp_path, monkeypatch
):
    """The bytes that are extracted define the snapshot, and they are unique."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    real_read = source_scanner.read_local_file
    calls = {"n": 0}

    def swapping_read(locator, policy, limits):
        calls["n"] += 1
        data = real_read(locator, policy, limits)
        if calls["n"] == 1:  # the extraction read follows the enumeration
            target.write_text("zweite Fassung", encoding="utf-8")
        return data

    monkeypatch.setattr("organiser.services.resources.snapshots.read_local_file", swapping_read)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(session, source_id, settings)
        assert result.counters.created == 1
        close_boundary(session)
        resource = session.scalar(select(Resource))
        rows = snapshots_of(session, resource.id)
        assert len(rows) == 1
        assert rows[0].content_hash == resource.current_content_hash
        assert rows[0].status == "ready"


def test_growing_file_is_refused_instead_of_being_read_without_bound(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("a" * 100, encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    real_read = os.read

    def growing_read(fd, size):
        chunk = real_read(fd, size)
        if chunk:
            with open(target, "a", encoding="utf-8") as handle:
                handle.write("b" * 100_000)
        return chunk

    monkeypatch.setattr(os, "read", growing_read)
    with pytest.raises(ResourceIngestionError) as error:
        source_scanner.read_local_file(str(target), policy)
    assert error.value.code == "file_changed_during_read"


def test_read_budget_is_not_overshot_by_one_file(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(4):
        (root / f"Notiz{index}.md").write_text("x" * 100, encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    limits = DEFAULT_LIMITS.narrowed(max_total_read_bytes=250, max_file_bytes=200)
    result = source_scanner.scan_source_tree(str(root), policy, limits)
    assert result.read_budget_used <= 250
    assert result.complete is False
    assert result.stop_reason == "read_budget_exhausted"


def test_unexpected_os_error_is_translated_and_leaves_a_closed_session(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))

    def exploding_walk(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr("organiser.services.resources.scan.scan_source_tree", exploding_walk)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        from organiser.domain.resources import SourceUnsafeError

        with pytest.raises(SourceUnsafeError) as error:
            scan_source(session, source_id, settings)
        assert error.value.code == "source_unsafe"
        assert session.in_transaction() is False
        close_boundary(session)
        run = session.scalar(select(ProcessingRun))
        assert run.status == "failed"
        assert run.output_payload["error_code"] == "source_unsafe"
