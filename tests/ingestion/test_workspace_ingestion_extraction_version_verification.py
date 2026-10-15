"""Version verification contracts for ingestion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from organiser.db.models import Event, ProcessingRun, Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    ResourceVersionMismatchError,
    SnapshotStatus,
)
from organiser.services import source_scanner
from organiser.services.resources import (
    assert_resource_version,
    extract_resource,
    get_resource_snapshot,
    verify_resource_version,
)
from support.ingestion import (
    close_boundary,
    make_pdf_bytes,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)
from support.workspace_ingestion_extraction import (
    PDF_AVAILABLE as PDF_AVAILABLE,
    requires_pypdf as requires_pypdf,
    course as course,
    scan_and_get_resource_id as scan_and_get_resource_id,
    hostile_worker as hostile_worker,
    _workdirs as _workdirs,
)


def test_unknown_resource_id_is_a_coded_404(course):
    settings, _context_id, _source_id, _root = course
    from organiser.domain.resources import ResourceNotFoundError

    with session_for(settings) as session:
        with pytest.raises(ResourceNotFoundError) as error:
            get_resource_snapshot(session, "gibt-es-nicht")
        assert error.value.status_code == 404
        assert error.value.as_payload()["code"] == "resource_not_found"


def test_pdf_extraction_end_to_end(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Vorlesung.pdf").write_bytes(make_pdf_bytes(["Bilanzierung Grundlagen"]))
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        close_boundary(session)
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        from organiser.services.resources import scan_source

        result = scan_source(session, source_id, settings)
        assert result.counters.created == 1
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Vorlesung.pdf"
        ).id
        close_boundary(session)
        extraction_result = extract_resource(session, resource_id, settings)
        if not PDF_AVAILABLE:
            assert extraction_result.status is SnapshotStatus.UNSUPPORTED
            pytest.skip("pypdf ist nicht installiert")
        assert extraction_result.status is SnapshotStatus.READY
        assert extraction_result.page_count == 1
        detail = get_resource_snapshot(session, resource_id)
        assert "Bilanzierung Grundlagen" in detail.current_snapshot.text
        assert detail.current_snapshot.metadata_payload["pages"][0]["page"] == 1


# -- freshness check ---------------------------------------------------------


def test_verify_accepts_the_current_version(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        detail = get_resource_snapshot(session, resource_id)
        expected = detail.current_snapshot.content_hash
    with session_for(settings) as session:
        check = verify_resource_version(session, resource_id, expected, settings)
        assert check.ok is True
        assert check.actual_hash == expected
        assert check.reason is None
        assert check.resource_status == "active"
        assert check.source_status == "active"
        assert assert_resource_version(session, resource_id, expected, settings).ok is True


def test_verify_rejects_a_stale_hash_without_writing(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    (root / "Notiz.md").write_text("abweichender Inhalt", encoding="utf-8")
    with session_for(settings) as session:
        before = len(snapshots_of(session, resource_id))
        runs_before = len(list(session.scalars(select(ProcessingRun))))
        events_before = len(list(session.scalars(select(Event))))
        close_boundary(session)
        check = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert check.ok is False
        assert check.reason == "hash_mismatch"
        assert check.actual_hash is not None and check.actual_hash != "0" * 64
        assert len(snapshots_of(session, resource_id)) == before
        assert len(list(session.scalars(select(ProcessingRun)))) == runs_before
        assert len(list(session.scalars(select(Event)))) == events_before
        close_boundary(session)
        with pytest.raises(ResourceVersionMismatchError):
            assert_resource_version(session, resource_id, "0" * 64, settings)


def test_verify_reports_a_missing_and_an_archived_resource(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        expected = get_resource_snapshot(session, resource_id).current_snapshot.content_hash
    (root / "Notiz.md").unlink()
    with session_for(settings) as session:
        resource = session.get(Resource, resource_id)
        resource.status = "missing"
        session.commit()
        close_boundary(session)
        check = verify_resource_version(session, resource_id, expected, settings)
        assert check.ok is False
        assert check.reason == "resource_missing"


def test_verify_reports_a_revoked_allowlist(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        expected = get_resource_snapshot(session, resource_id).current_snapshot.content_hash
    narrowed = source_scanner.IngestionPolicy(allowed_roots=(str(root.parent / "anders"),))
    with session_for(settings) as session:
        check = verify_resource_version(session, resource_id, expected, settings, policy=narrowed)
        assert check.ok is False
        assert check.reason == "source_not_allowed"


def test_verify_does_not_claim_an_old_snapshot_as_current(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    (root / "Notiz.md").write_text("neue Fassung ohne Snapshot", encoding="utf-8")
    with session_for(settings) as session:
        from organiser.services.resources import scan_source

        scan_source(session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_extractions=0))
        close_boundary(session)
        # The registry knows the new hash, but no snapshot describes it yet.
        detail = get_resource_snapshot(session, resource_id)
        assert detail.resource.has_content_hash is True
        assert detail.snapshot_is_stale is True
        assert detail.stale_reason == "no_snapshot_for_current_hash"
        assert detail.current_snapshot is None
        assert detail.snapshot_count == 1


def test_cross_context_resource_is_not_reachable_through_another_context(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        first_context = seed_context(session).id
        close_boundary(session)
        second_context = seed_context(session).id
        first_source = seed_source_id(session, first_context, path=str(root))
        close_boundary(session)
        second_source = seed_source_id(session, second_context, path=str(root))
        close_boundary(session)
        from organiser.services.resources import list_resources, scan_source

        scan_source(session, first_source, settings)
        scan_source(session, second_source, settings)
        first_page = list_resources(session, first_context)
        second_page = list_resources(session, second_context)
        assert {item.id for item in first_page.items}.isdisjoint(
            {item.id for item in second_page.items}
        )
        assert all(item.context_id == first_context for item in first_page.items)
        assert all(item.context_id == second_context for item in second_page.items)
