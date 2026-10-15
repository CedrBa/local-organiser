"""Document budgets contracts for ingestion."""

from __future__ import annotations

import os
import pytest
from sqlalchemy import select
from organiser.db.models import Event, Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    ResourceIngestionError,
)
from organiser.services import source_scanner
from organiser.services.resources import (
    REASON_FILE_UNREADABLE,
    REASON_RESOURCE_EMPTY,
    extract_resource,
    get_resource_snapshot,
    list_resources,
    scan_source,
    verify_resource_version,
)
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)
from support.workspace_ingestion_review_fixes import (
    EMPTY_SHA256 as EMPTY_SHA256,
    _CountingScanner as _CountingScanner,
    _scanned_course as _scanned_course,
    _resource_id_of as _resource_id_of,
    _refill_after_stat as _refill_after_stat,
    _orphan_the_source as _orphan_the_source,
    resource_id_of as resource_id_of,
)


def test_entry_budget_of_zero_makes_a_document_source_incomplete(tmp_path):
    """A lowered entry budget is reported before the single file is read."""
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        folder_id = seed_source_id(session, context_id, path=str(root))
        document_id = seed_source_id(
            session, context_id, path=str(root / "Notiz.md"), source_type="document"
        )
        close_boundary(session)
        zero = DEFAULT_LIMITS.narrowed(max_entries=0)
        for source_id in (document_id, folder_id):
            result = scan_source(session, source_id, settings, limits=zero)
            assert result.complete is False
            assert result.error_code == "entry_limit_reached"
            assert result.counters.created == 0
            assert result.counters.entries == 0
            assert result.counters.marked_missing == 0
            close_boundary(session)
        assert list_resources(session, context_id).total == 0


def test_short_read_is_reported_as_a_short_read_not_as_a_budget(tmp_path, monkeypatch):
    """The direct reader and the enumeration name the same cause identically."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def short_read(fd, size):
        return b"" if fd in opened else b""

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "read", short_read)
    with pytest.raises(ResourceIngestionError) as error:
        source_scanner.read_local_file(str(target), policy)
    assert error.value.code == "file_short_read"

    result = source_scanner.scan_source_tree(str(root), policy)
    assert result.files[0].content_hash is None
    assert result.files[0].unread_reason == "file_short_read"


def test_no_current_text_after_the_scan_budget_is_exceeded(tmp_path):
    """Budget exhaustion withdraws the currency as well, and keeps the history."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        before = session.get(Resource, resource_id)
        before_hash = before.current_content_hash
        close_boundary(session)
        target.write_text("x" * 4096, encoding="utf-8")
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_file_bytes=128)
        )
        close_boundary(session)
        assert result.complete is True
        assert any(warning.code == "file_too_large" for warning in result.warnings)
        after = session.get(Resource, resource_id)
        assert after.current_content_hash is None
        assert after.byte_size == before.byte_size
        assert get_resource_snapshot(session, resource_id).current_snapshot is None
        assert [row.content_hash for row in snapshots_of(session, resource_id)] == [before_hash]
        close_boundary(session)
        scan_source(session, source_id, settings)
        close_boundary(session)
        assert session.get(Resource, resource_id).current_content_hash is not None


def test_verify_never_persists_an_invalidation(tmp_path):
    """The freshness check is read only: it may not withdraw a version."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        current_hash = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)

        target.write_bytes(b"")
        check = verify_resource_version(session, resource_id, current_hash, settings)
        assert check.ok is False
        assert check.reason == REASON_RESOURCE_EMPTY
        close_boundary(session)
        assert session.get(Resource, resource_id).current_content_hash == current_hash
        assert len(snapshots_of(session, resource_id)) == 1
        assert [
            event
            for event in session.scalars(select(Event))
            if event.payload.get("version_invalidated")
        ] == []

        target.write_text("erste Fassung", encoding="utf-8")
        target.chmod(0o000)
        close_boundary(session)
        try:
            check = verify_resource_version(session, resource_id, current_hash, settings)
        finally:
            target.chmod(0o600)
        assert check.ok is False
        assert check.reason == REASON_FILE_UNREADABLE
        close_boundary(session)
        assert session.get(Resource, resource_id).current_content_hash == current_hash


def test_document_source_resource_can_be_extracted_and_verified(tmp_path):
    """The source *is* its file, so the address check must accept it."""
    root = tmp_path / "kurs"
    (root / "unter").mkdir(parents=True)
    target = root / "unter" / "Notiz.md"
    target.write_text("erste Fassung der Notiz", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(target), source_type="document")
        scan_source(session, source_id, settings)
        resource_id = _resource_id_of(session, context_id)
        close_boundary(session)

        outcome = extract_resource(session, resource_id, settings)
        assert outcome.status.value == "ready"
        close_boundary(session)
        check = verify_resource_version(session, resource_id, outcome.content_hash, settings)
        assert check.ok is True
        assert check.actual_hash == outcome.content_hash


# -- F8: an exhausted read budget is not a proven scope, for any source type ---


def test_a_document_source_stops_incomplete_when_its_read_budget_is_spent(tmp_path):
    """The whole scope of a document source is one file, so this matters most here.

    A folder walk that runs out of read budget reports ``complete=False``. A single
    file used to claim ``complete=True`` no matter why it had no hash, which is the
    one place where the claim is most tempting and most wrong: the entire scope of
    this source is that file, and the scan never got to look at its bytes. A
    complete run is allowed to conclude absence, so the difference between the two
    answers is a ``missing`` decision made from a scan that stopped short.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("x" * 200, encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))

    # No budget at all, and a budget smaller than the file: both are "did not read".
    for budget in (0, 50):
        result = source_scanner.scan_source_tree(
            str(target),
            policy,
            DEFAULT_LIMITS.narrowed(max_total_read_bytes=budget, max_file_bytes=200),
            expect="file",
        )
        assert result.complete is False
        assert result.stop_reason == "read_budget_exhausted"
        assert result.files[0].unread_reason == "read_budget_exhausted"
        assert result.files[0].content_hash is None

    # A file that is merely too large was still located and read enough to describe,
    # so that reason keeps the run complete. Only the budget says "not seen".
    oversized = source_scanner.scan_source_tree(
        str(target), policy, DEFAULT_LIMITS.narrowed(max_file_bytes=10), expect="file"
    )
    assert oversized.complete is True
    assert oversized.stop_reason is None
    assert oversized.files[0].unread_reason == "file_too_large"

    # And the same question asked the other way round stays complete as well.
    whole = source_scanner.scan_source_tree(str(target), policy, expect="file")
    assert whole.complete is True
    assert whole.stop_reason is None


def test_a_budget_stopped_document_scan_never_draws_a_missing_decision(tmp_path):
    """The scanner level fix has to reach the run, counters and all."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        document_id = seed_source_id(session, context_id, path=str(target), source_type="document")
        close_boundary(session)
        result = scan_source(
            session, document_id, settings, limits=DEFAULT_LIMITS.narrowed(max_total_read_bytes=0)
        )
        assert result.complete is False
        assert result.error_code == "read_budget_exhausted"
        assert result.counters.marked_missing == 0
        assert any(
            warning.code == "scan_incomplete_no_missing_decision" for warning in result.warnings
        )
        close_boundary(session)
        # The file was located, so the resource exists and is visible -- but it has
        # no version, because no byte of it was ever read, and nothing was marked
        # missing.
        page = list_resources(session, context_id)
        assert page.total == 1
        assert page.items[0].current_snapshot_id is None
        assert result.counters.marked_missing == 0
        assert "file_unreadable" not in {warning.code for warning in result.warnings}

        # A folder source with the same spent budget agrees.
        folder_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        folder = scan_source(
            session, folder_id, settings, limits=DEFAULT_LIMITS.narrowed(max_total_read_bytes=0)
        )
        assert folder.complete is False
        assert folder.error_code == "read_budget_exhausted"
        close_boundary(session)
