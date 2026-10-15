"""Empty versions contracts for ingestion."""

from __future__ import annotations

import os
from sqlalchemy import select
from organiser.db.models import Event, Resource
from organiser.services.resources import (
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


# -- F1: a zero-length observation is never a version --------------------------


def test_truncate_then_write_withdraws_the_current_version_and_keeps_the_history(tmp_path):
    """A writer that truncated but has not refilled yet invalidates the currency.

    At the moment of the scan the file really is zero bytes long. Keeping the old
    hash would let every caller keep quoting the previously extracted text as the
    current state of a document whose current bytes nobody has read. Withdrawing
    the currency is the honest answer; deleting the history would not be, and does
    not happen: the immutable snapshot of the version that was really read stays.
    """
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
        before_hash, before_size, before_mtime = (
            before.current_content_hash,
            before.byte_size,
            before.mtime_ns,
        )
        history_before = snapshots_of(session, resource_id)
        assert [row.content_hash for row in history_before] == [before_hash]
        assert history_before[0].status == "ready"
        close_boundary(session)

        os.truncate(target, 0)
        result = scan_source(session, source_id, settings)
        close_boundary(session)

        assert before_hash not in (None, EMPTY_SHA256)
        assert result.complete is True
        assert any(warning.code == "empty_file" for warning in result.warnings)
        resource = session.get(Resource, resource_id)
        assert resource.current_content_hash is None
        assert resource.status == "active"
        # Size and mtime keep describing the last version that was measured with its
        # hash; they are not moved onto bytes that were never read.
        assert resource.byte_size == before_size
        assert resource.mtime_ns == before_mtime
        # Nothing is deleted: the historical snapshot is intact and still ready.
        history_after = snapshots_of(session, resource_id)
        assert [row.content_hash for row in history_after] == [before_hash]
        assert history_after[0].status == "ready"
        assert history_after[0].text is not None
        # And it is no longer presented as the current text of the document.
        detail = get_resource_snapshot(session, resource_id)
        assert detail.current_snapshot is None
        assert detail.snapshot_is_stale is True
        assert detail.stale_reason == "no_snapshot_for_current_hash"
        assert detail.resource.has_content_hash is False
        assert list_resources(session, context_id).items[0].current_snapshot_id is None
        invalidated = [
            event
            for event in session.scalars(select(Event).where(Event.entity_id == resource_id))
            if event.payload.get("version_invalidated") is True
        ]
        assert len(invalidated) == 1
        assert invalidated[0].payload["unread_reason"] == "empty_file"

        # The writer finishes its work, and a fresh observation restores currency.
        close_boundary(session)
        target.write_text("zweite Fassung", encoding="utf-8")
        second = scan_source(session, source_id, settings)
        assert second.counters.refreshed == 1
        restored = session.get(Resource, resource_id)
        assert restored.current_content_hash not in (None, EMPTY_SHA256, before_hash)
        assert restored.byte_size == len("zweite Fassung")
        detail = get_resource_snapshot(session, resource_id)
        assert detail.snapshot_is_stale is False
        assert detail.current_snapshot.text.startswith("zweite Fassung")


def test_write_landing_during_the_observation_never_becomes_the_empty_version(
    tmp_path, monkeypatch
):
    """Same property when the refill lands while the scan is looking at the file."""
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
        before_hash = session.get(Resource, resource_id).current_content_hash
        snapshots_before = len(snapshots_of(session, resource_id))
        close_boundary(session)

        monkeypatch.setattr(os, "fstat", _refill_after_stat(target))
        scan_source(session, source_id, settings)
        close_boundary(session)

        resource = session.get(Resource, resource_id)
        assert resource.current_content_hash is None
        assert resource.current_content_hash != EMPTY_SHA256
        assert len(snapshots_of(session, resource_id)) == snapshots_before


def test_genuinely_empty_file_stays_visible_and_gets_no_version(tmp_path):
    """An empty file is a real file: visible, active, and simply without a version."""
    root = tmp_path / "kurs"
    root.mkdir()
    empty = root / "Notiz.md"
    empty.touch()
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(session, source_id, settings)
        assert result.complete is True
        assert result.counters.created == 1
        assert any(warning.code == "empty_file" for warning in result.warnings)
        resource = resource_by_locator(session, context_id=context_id, relative_path="Notiz.md")
        assert resource.status == "active"
        assert resource.current_content_hash is None
        assert snapshots_of(session, resource.id) == []
        # The file is listed, so it is not "unknown" and not missing either.
        page = list_resources(session, context_id)
        assert page.total == 1 and page.items[0].has_content_hash is False
        close_boundary(session)
        empty.write_text("jetzt mit Inhalt", encoding="utf-8")
        scan_source(session, source_id, settings)
        close_boundary(session)
        assert resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).current_content_hash


def test_extraction_of_an_empty_original_withdraws_the_current_version(tmp_path):
    """An explicit extraction of an empty file invalidates and leaves a trace."""
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
        before_hash, before_size, before_mtime = (
            before.current_content_hash,
            before.byte_size,
            before.mtime_ns,
        )
        close_boundary(session)
        target.write_bytes(b"")

        outcome = extract_resource(session, resource_id, settings)
        assert outcome.status.value == "failed"
        assert outcome.error_code == "empty_file"
        close_boundary(session)

        resource = session.get(Resource, resource_id)
        assert resource.current_content_hash is None
        assert resource.byte_size == before_size
        assert resource.mtime_ns == before_mtime
        # The failure is visible, the version that was really read is not deleted,
        # and it is no longer offered as the current text.
        detail = get_resource_snapshot(session, resource_id)
        assert any(snapshot.error_code == "empty_file" for snapshot in detail.snapshots)
        assert any(
            snapshot.content_hash == before_hash and snapshot.status == "ready"
            for snapshot in detail.snapshots
        )
        assert detail.current_snapshot is None
        assert detail.stale_reason == "no_snapshot_for_current_hash"
        invalidated = [
            event
            for event in session.scalars(select(Event).where(Event.entity_id == resource_id))
            if event.payload.get("version_invalidated") is True
        ]
        assert len(invalidated) == 1
        assert invalidated[0].payload["observed_during"] == "resource_extraction"

        # A fresh read of real content restores both currency and the text.
        close_boundary(session)
        target.write_text("zweite Fassung", encoding="utf-8")
        outcome = extract_resource(session, resource_id, settings)
        assert outcome.status.value == "ready"
        close_boundary(session)
        detail = get_resource_snapshot(session, resource_id)
        assert detail.snapshot_is_stale is False
        assert detail.current_snapshot.text == "zweite Fassung"


def test_empty_original_never_verifies(tmp_path):
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
        for expected in (current_hash, EMPTY_SHA256):
            close_boundary(session)
            check = verify_resource_version(session, resource_id, expected, settings)
            assert check.ok is False
            assert check.reason == REASON_RESOURCE_EMPTY
