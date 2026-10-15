"""Read versions contracts for ingestion."""

from __future__ import annotations

import os
import pytest
from organiser.db.models import Resource
from organiser.domain.resources import (
    ResourceIngestionError,
)
from organiser.services import source_scanner
from organiser.services.resources import (
    get_resource_snapshot,
    list_resources,
    scan_source,
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


def test_short_read_is_refused_instead_of_becoming_the_empty_version(tmp_path, monkeypatch):
    """A reader that returns fewer bytes than ``st_size`` promises is a refusal.

    ``os.read`` on a regular file only returns nothing at end of file. If the
    descriptor hands out nothing while the file still claims to have bytes, the
    bytes that were read are not the file, and the empty hash must not be stored
    as the document's current version.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    real_read = os.read
    opened: list[int] = []
    real_open = os.open

    def tracking_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def short_read(fd, size):
        if fd in opened:
            return b""
        return real_read(fd, size)

    monkeypatch.setattr(os, "open", tracking_open)
    monkeypatch.setattr(os, "read", short_read)
    with pytest.raises(ResourceIngestionError) as error:
        source_scanner.read_local_file(str(target), policy)
    assert error.value.code == "file_short_read"
    assert error.value.context["read_bytes"] == 0
    assert error.value.context["expected_bytes"] == len("erste Fassung")


def test_short_read_during_enumeration_leaves_the_previous_version(tmp_path, monkeypatch):
    """The file changed *and* could not be read completely: nothing moves."""
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
        before_values = (before.current_content_hash, before.byte_size, before.mtime_ns)
        close_boundary(session)
        target.write_text("zweite, deutlich laengere Fassung", encoding="utf-8")
        opened: list[int] = []
        real_open = os.open
        real_read = os.read

        def tracking_open(*args, **kwargs):
            fd = real_open(*args, **kwargs)
            opened.append(fd)
            return fd

        def short_read(fd, size):
            if fd in opened:
                return b""
            return real_read(fd, size)

        monkeypatch.setattr(os, "open", tracking_open)
        monkeypatch.setattr(os, "read", short_read)
        result = scan_source(session, source_id, settings)
        close_boundary(session)
        assert result.complete is True
        resource = session.get(Resource, resource_id)
        # The cause is the descriptor, not our budget: the file announced 35 bytes
        # and the read budget was nowhere near spent.
        assert any(warning.code == "file_short_read" for warning in result.warnings)
        assert any(warning.code == "read_budget_exhausted" for warning in result.warnings) is False
        assert resource.current_content_hash is None
        assert resource.byte_size == before_values[1]
        assert resource.mtime_ns == before_values[2]


def test_real_rewrite_still_produces_a_new_version(tmp_path):
    """The empty policy must not turn the normal change detection into a no-op."""
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
        first = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)
        target.write_text("zweite Fassung", encoding="utf-8")
        result = scan_source(session, source_id, settings)
        close_boundary(session)
        assert result.counters.refreshed == 1
        resource = session.get(Resource, resource_id)
        assert resource.current_content_hash not in (first, EMPTY_SHA256)
        assert resource.byte_size == len("zweite Fassung")
        assert len(snapshots_of(session, resource_id)) == 2


# -- F2: version metadata changes together, or not at all -----------------------


def test_unreadable_file_withdraws_the_current_version_but_keeps_measured_metadata(tmp_path):
    """A changed *and* unreadable file invalidates the currency, not the history.

    Keeping the hash would let the caller quote the old text as the current state
    of a document nobody could read; moving the fresh size and mtime onto the row
    would make the row describe bytes that were never hashed.
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
        before_values = (before.current_content_hash, before.byte_size, before.mtime_ns)
        close_boundary(session)
        target.write_text("zweite, deutlich laengere Fassung", encoding="utf-8")
        target.chmod(0o000)
        try:
            result = scan_source(session, source_id, settings)
        finally:
            target.chmod(0o600)
        close_boundary(session)
        assert result.complete is True
        assert any(warning.code == "file_unreadable" for warning in result.warnings)
        after = session.get(Resource, resource_id)
        assert after.current_content_hash is None
        assert (after.byte_size, after.mtime_ns) == before_values[1:]
        assert after.status == "active"
        assert result.counters.marked_missing == 0
        detail = get_resource_snapshot(session, resource_id)
        assert detail.current_snapshot is None
        assert detail.stale_reason == "no_snapshot_for_current_hash"
        assert list_resources(session, context_id).items[0].current_snapshot_id is None
        assert [row.content_hash for row in snapshots_of(session, resource_id)] == [
            before_values[0]
        ]
        # The bytes are still there and still readable afterwards.
        close_boundary(session)
        result = scan_source(session, source_id, settings)
        close_boundary(session)
        assert result.counters.refreshed == 1
        assert session.get(Resource, resource_id).current_content_hash != before_values[0]
