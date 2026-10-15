"""Version refusals contracts for ingestion."""

from __future__ import annotations

import errno
import os
import pytest
from sqlalchemy import select
from organiser.db.models import Event, ProcessingRun, Resource
from organiser.domain.resources import (
    SourceFileChangedError,
)
from organiser.services import source_scanner
from organiser.services.resources import (
    REASON_FILE_UNREADABLE,
    SourceIdentity,
    scan_source,
    verify_resource_version,
)
from organiser.services.source_scanner import ObservedFile
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


# -- F4: a failing read is a typed refusal of the freshness check --------------


def test_verify_translates_a_transient_read_error_and_writes_nothing(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        snapshots_before = len(snapshots_of(session, resource_id))
        events_before = len(list(session.scalars(select(Event))))
        runs_before = len(list(session.scalars(select(ProcessingRun))))
        close_boundary(session)

        def exploding_read(fd, size):
            raise OSError(errno.EIO, "Input/output error")

        monkeypatch.setattr(os, "read", exploding_read)
        check = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert check.ok is False
        assert check.reason == REASON_FILE_UNREADABLE
        assert check.context_id == context_id
        monkeypatch.undo()

        assert session.in_transaction() is False
        close_boundary(session)
        assert len(snapshots_of(session, resource_id)) == snapshots_before
        assert len(list(session.scalars(select(Event)))) == events_before
        assert len(list(session.scalars(select(ProcessingRun)))) == runs_before
        # The session is a clean boundary again, so the next service call works.
        close_boundary(session)
        result = scan_source(session, source_id, settings)
        assert result.complete is True


def test_verify_distinguishes_unauthorized_from_unreadable(tmp_path, monkeypatch):
    """The two reason codes that sound alike are decided by different facts.

    ``file_unreadable`` has to mean "the operator policy covers this address and
    the bytes still could not be read"; ``source_not_allowed`` has to mean "the
    policy does not cover this address" and is decided before a single byte is
    touched. The permission error is injected at ``os.open`` rather than produced
    with ``chmod``, so the classification is checked the same way for every user --
    ``chmod 000`` is a no-op for root, and a test that silently changes meaning
    there would prove nothing.
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
        expected = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)

        real_open = os.open

        def denying_open(path, *args, **kwargs):
            if path == target.name:
                raise PermissionError(errno.EACCES, "Permission denied")
            return real_open(path, *args, **kwargs)

        # 1. Allowed address, unreadable bytes: the read failed, not the policy.
        monkeypatch.setattr(os, "open", denying_open)
        denied = verify_resource_version(session, resource_id, expected, settings)
        assert denied.ok is False
        assert denied.reason == REASON_FILE_UNREADABLE
        close_boundary(session)

        # 2. The very same refusal in the enumeration is the same reason code, so
        #    both readers of one file describe one cause identically.
        monkeypatch.setattr(os, "open", denying_open)
        tree = source_scanner.scan_source_tree(
            str(root), source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path),))
        )
        assert [item.unread_reason for item in tree.files] == [REASON_FILE_UNREADABLE]
        monkeypatch.undo()
        close_boundary(session)

        # 3. Allowed address that does verify: the injection was the only cause.
        readable = verify_resource_version(session, resource_id, expected, settings)
        assert readable.ok is True
        close_boundary(session)

        # 4. A revoked allowlist is the other condition and keeps its own code.
        revoked = source_scanner.IngestionPolicy(allowed_roots=(str(tmp_path / "anders"),))
        unauthorized = verify_resource_version(
            session, resource_id, expected, settings, policy=revoked
        )
        assert unauthorized.ok is False
        assert unauthorized.reason == "source_not_allowed"
        assert unauthorized.reason != REASON_FILE_UNREADABLE


def test_file_changed_during_read_is_typed_for_a_non_empty_file(tmp_path, monkeypatch):
    """The existing refusal keeps its code; the empty case is a separate reason."""
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("erste Fassung", encoding="utf-8")
    policy = source_scanner.IngestionPolicy(allowed_roots=(str(root),))
    real_read = os.read

    def swapping_read(fd, size):
        chunk = real_read(fd, size)
        if chunk:
            target.write_text("andere Fassung", encoding="utf-8")
        return chunk

    monkeypatch.setattr(os, "read", swapping_read)
    with pytest.raises(SourceFileChangedError):
        source_scanner.read_local_file(str(target), policy)


def test_verify_refuses_a_file_that_changes_during_the_read(tmp_path, monkeypatch):
    settings, context_id, source_id, root = _scanned_course(tmp_path)
    with session_for(settings) as session:
        resource_id = _resource_id_of(session, context_id)
        snapshots_before = len(snapshots_of(session, resource_id))
        close_boundary(session)
        target = root / "Notiz.md"
        real_read = os.read

        def swapping_read(fd, size):
            chunk = real_read(fd, size)
            if chunk:
                target.write_text("andere Fassung", encoding="utf-8")
            return chunk

        monkeypatch.setattr(os, "read", swapping_read)
        check = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert check.ok is False
        assert check.reason == "file_changed_during_read"
        monkeypatch.undo()
        close_boundary(session)
        assert len(snapshots_of(session, resource_id)) == snapshots_before


def test_rebind_guards_refuse_a_foreign_address_and_a_denied_policy():
    """The defensive guards of the re-adoption, checked directly.

    Through the scan path both are unreachable -- a locator always comes from the
    source that just enumerated it -- so they are exercised here on purpose instead
    of being left untested.
    """
    from organiser.services.resources import _rebind_orphan_resource

    policy = source_scanner.IngestionPolicy(allowed_roots=("/freigegeben",))
    source = SourceIdentity(
        source_id="quelle-1",
        context_id="context-1",
        source_type="folder",
        path="/freigegeben/kurs",
        status="active",
        locator="/freigegeben/kurs",
    )
    outside = ObservedFile(
        relative_path="anders/x.md",
        locator="/anders/x.md",
        title="x",
        resource_type="markdown",
        byte_size=3,
        mtime_ns=1,
        content_hash="a" * 64,
    )
    denied = ObservedFile(
        relative_path="kurs/.versteckt.md",
        locator="/freigegeben/kurs/.versteckt.md",
        title="versteckt",
        resource_type="markdown",
        byte_size=3,
        mtime_ns=1,
        content_hash="b" * 64,
    )
    owned = ObservedFile(
        relative_path="kurs/x.md",
        locator="/freigegeben/kurs/x.md",
        title="x",
        resource_type="markdown",
        byte_size=3,
        mtime_ns=1,
        content_hash="c" * 64,
    )
    orphan = Resource(
        context_id="context-1", source_id=None, locator="/freigegeben/kurs/x.md", status="active"
    )

    assert (
        _rebind_orphan_resource(orphan, outside, source, policy=policy) == "address_outside_source"
    )
    assert _rebind_orphan_resource(orphan, denied, source, policy=policy) == "policy_denied"
    assert orphan.source_id is None
    assert _rebind_orphan_resource(orphan, owned, source, policy=policy) == "rebound"
    assert orphan.source_id == "quelle-1"
    # A row that has an owner is never touched again.
    assert _rebind_orphan_resource(orphan, owned, source, policy=policy) is None
