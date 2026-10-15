"""Overlapping sources contracts for ingestion."""

from __future__ import annotations

import errno
import os
from organiser.db.models import Resource
from organiser.domain.resources import (
    CODE_SOURCE_NOT_ALLOWED,
    CODE_SOURCE_UNSAFE,
    DEFAULT_LIMITS,
    SourceAccessError,
    SourceFileChangedError,
    SourceUnsafeError,
)
from organiser.services.resources import (
    REASON_FILE_UNREADABLE,
    _read_refusal_reason,
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


# -- F9: only the owning source may withdraw the version -----------------------


def test_a_foreign_duplicate_cannot_withdraw_the_owners_version(tmp_path, monkeypatch):
    """Two sources see one address; only the owner decides its currency.

    A duplicate source that fails to read the bytes has learned nothing about
    them. Before the fix that failure withdrew the current hash the *owner* had
    just measured, so a neighbouring source could retire the currency of a
    document it had never successfully read -- by spending its own budget, or by
    hitting a transient EACCES. The registry then reported "no current version"
    for a document that was perfectly readable, and the caller's text fallback
    went quiet.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung der Notiz", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        # The document source owns the row: it registered it first, under its own
        # scope, and it is the source whose currency is at stake below.
        document_id = seed_source_id(session, context_id, path=str(target), source_type="document")
        folder_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, document_id, settings)
        close_boundary(session)
        resource = resource_by_locator(session, context_id=context_id, relative_path="Notiz.md")
        resource_id = resource.id
        owned_hash = resource.current_content_hash
        assert owned_hash is not None
        close_boundary(session)

        # The folder source is the duplicate here, and its scan cannot read a byte.
        result = scan_source(
            session, folder_id, settings, limits=DEFAULT_LIMITS.narrowed(max_total_read_bytes=0)
        )
        assert result.counters.duplicates == 1
        close_boundary(session)
        row = session.get(Resource, resource_id)
        assert row.current_content_hash == owned_hash
        assert row.source_id == document_id
        # The caller's current text is still available, because the version stands.
        assert list_resources(session, context_id).items[0].current_snapshot_id is not None

        # A transient permission failure of the foreign source is equally powerless.
        close_boundary(session)
        real_open = os.open

        def denying_open(path, *args, **kwargs):
            if path == target.name:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denying_open)
        denied = scan_source(session, folder_id, settings)
        monkeypatch.undo()
        close_boundary(session)
        assert denied.counters.duplicates == 1
        assert any(warning.code == REASON_FILE_UNREADABLE for warning in denied.warnings)
        assert session.get(Resource, resource_id).current_content_hash == owned_hash

        # The owner still withdraws it itself: the gate is ownership, not inaction.
        # A narrowed per-file limit makes this a hashless observation rather than
        # an empty scope, so it exercises the same branch the foreign source missed.
        close_boundary(session)
        target.write_text("zweite Fassung", encoding="utf-8")
        owner_result = scan_source(
            session, document_id, settings, limits=DEFAULT_LIMITS.narrowed(max_file_bytes=4)
        )
        close_boundary(session)
        assert any(warning.code == "file_too_large" for warning in owner_result.warnings)
        assert session.get(Resource, resource_id).current_content_hash is None
        # The history still holds the version that really was read.
        assert [row.content_hash for row in snapshots_of(session, resource_id)] == [owned_hash]


def test_a_foreign_duplicate_may_still_refresh_the_version(tmp_path):
    """The gate is on withdrawal only, not on every write.

    A hash is a property of the file, not of the row's owner: two sources that
    both read the same bytes measured the same version. Blocking that too would
    make the result depend on scan order for no reason.
    """
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "unter" / "Notiz.md"
    target.parent.mkdir()
    target.write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        folder_id = seed_source_id(session, context_id, path=str(root))
        document_id = seed_source_id(session, context_id, path=str(target), source_type="document")
        scan_source(session, folder_id, settings)
        close_boundary(session)
        resource = resource_by_locator(
            session, context_id=context_id, relative_path="unter/Notiz.md"
        )
        resource_id = resource.id
        first_hash = resource.current_content_hash
        close_boundary(session)

        target.write_text("zweite, deutlich laengere Fassung der Notiz", encoding="utf-8")
        result = scan_source(session, document_id, settings)
        assert result.counters.duplicates == 1
        close_boundary(session)
        row = session.get(Resource, resource_id)
        assert row.source_id == folder_id
        assert row.current_content_hash is not None
        assert row.current_content_hash != first_hash
        # The row still describes the version the owner measured, not a foreign label.
        assert row.relative_path == "unter/Notiz.md"
        assert row.title == "Notiz"


# -- F10: one EACCES, one reason code, in the scan's extraction path ------------


def test_the_scan_names_an_extraction_read_failure_the_same_way_the_check_does(
    tmp_path, monkeypatch
):
    """Both readers of one file use one code for one cause.

    The extraction read of a scan runs after the enumeration has already
    authorised the address, so an ``EACCES`` here can only mean "the policy covers
    this and the bytes still would not come". Reporting ``source_not_allowed``
    instead pointed an operator at an allowlist that never changed, while the
    enumeration of the very same file had already said ``file_unreadable``.
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
        resource_id = _resource_id_of(session, context_id)
        first_hash = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)

        real_open = os.open

        def always_denying_open(path, *args, **kwargs):
            if path == target.name:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *args, **kwargs)

        # Changed content, so an extraction really is planned. The enumeration's
        # open is the first one and succeeds; the read that produces the snapshot is
        # the second one and is refused.
        target.write_text("zweite, deutlich laengere Fassung der Notiz", encoding="utf-8")
        opened: list[str] = []

        def denying_second_open(path, *args, **kwargs):
            if path == target.name:
                opened.append(str(path))
                if len(opened) > 1:
                    raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", denying_second_open)
        result = scan_source(session, source_id, settings)
        close_boundary(session)

        assert len(opened) == 2, "the extraction read has to be the one that failed"
        codes = {warning.code for warning in result.warnings}
        assert REASON_FILE_UNREADABLE in codes
        assert "source_not_allowed" not in codes
        # No snapshot of the new version was stored, and the enumeration's version stands.
        assert len(snapshots_of(session, resource_id)) == 1
        assert session.get(Resource, resource_id).current_content_hash != first_hash

        # The freshness check reports the identical refusal identically. Every open
        # of the file is denied here, which is the same condition seen from the other
        # reader, and the code is the same one.
        expected = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)
        monkeypatch.setattr(os, "open", always_denying_open)
        denied = verify_resource_version(session, resource_id, expected, settings)
        monkeypatch.undo()
        assert denied.ok is False
        assert denied.reason == REASON_FILE_UNREADABLE
        assert denied.reason in codes

    # The helper is the single place that decides it, for a typed refusal and for a
    # raw OSError alike.
    assert (
        _read_refusal_reason(
            SourceAccessError("x", code=CODE_SOURCE_NOT_ALLOWED, reason="permission")
        )
        == REASON_FILE_UNREADABLE
    )
    assert _read_refusal_reason(OSError(errno.EIO, "Input/output error")) == REASON_FILE_UNREADABLE
    # Every other refusal keeps its own code, so the mapping does not flatten causes.
    assert _read_refusal_reason(SourceFileChangedError("x")) == "file_changed_during_read"
    assert (
        _read_refusal_reason(SourceUnsafeError("x", code=CODE_SOURCE_UNSAFE, reason="symlink"))
        == CODE_SOURCE_UNSAFE
    )
    # A genuine revocation keeps its code here too.
    assert (
        _read_refusal_reason(
            SourceAccessError("x", code=CODE_SOURCE_NOT_ALLOWED, reason="outside_allowed_roots")
        )
        == CODE_SOURCE_NOT_ALLOWED
    )
