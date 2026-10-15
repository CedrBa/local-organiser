"""Source ownership contracts for ingestion."""

from __future__ import annotations

from sqlalchemy import select
from organiser.db.models import Event, Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
)
from organiser.services.resources import (
    extract_resource,
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


def test_orphan_resource_is_re_adopted_by_a_fresh_scan(tmp_path):
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
        close_boundary(session)

        _orphan_the_source(session, source_id)
        close_boundary(session)
        assert session.get(Resource, resource_id).source_id is None
        # Without a re-adoption the row would be stuck forever.
        close_boundary(session)
        stale = verify_resource_version(session, resource_id, "0" * 64, settings)
        assert stale.ok is False and stale.reason == "source_removed"
        close_boundary(session)

        successor = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        result = scan_source(session, successor, settings)
        assert result.counters.rebound == 1
        assert result.counters.created == 0
        close_boundary(session)

        resource = session.get(Resource, resource_id)
        assert resource.source_id == successor
        assert resource.source_path_snapshot == str(root)
        assert resource.current_content_hash not in (None, EMPTY_SHA256)
        rebound_events = [
            event
            for event in session.scalars(
                select(Event).where(
                    Event.entity_id == resource_id,
                    Event.event_type == "RESOURCE_REFRESHED",
                )
            )
            if event.payload.get("rebound_source") is True
        ]
        assert len(rebound_events) == 1
        assert str(root) not in str(rebound_events[0].payload)

        # Access works again, and absence can finally be decided.
        close_boundary(session)
        outcome = extract_resource(session, resource_id, settings)
        assert outcome.status.value == "ready"
        close_boundary(session)
        assert (
            verify_resource_version(session, resource_id, outcome.content_hash, settings).ok is True
        )
        close_boundary(session)
        target.unlink()
        closing = scan_source(session, successor, settings)
        assert closing.counters.marked_missing == 1
        close_boundary(session)
        assert session.get(Resource, resource_id).status == "missing"


def test_re_adoption_never_takes_a_valid_foreign_owner(tmp_path):
    first = tmp_path / "erster"
    second = tmp_path / "zweiter"
    first.mkdir()
    second.mkdir()
    (first / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    (second / "Notiz.md").write_text("zweite Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(session, context_id, path=str(first))
        second_id = seed_source_id(session, context_id, path=str(second))
        scan_source(session, first_id, settings)
        close_boundary(session)
        row = session.get(Resource, resource_id_of(session, context_id, first / "Notiz.md"))
        row.source_id = second_id  # a valid, active owner that is not the scanned source
        session.commit()
        close_boundary(session)

        result = scan_source(session, first_id, settings)
        assert result.counters.rebound == 0
        assert result.counters.duplicates == 1
        close_boundary(session)
        assert session.get(Resource, row.id).source_id == second_id


def test_re_adoption_leaves_an_archived_row_alone(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, first_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        _orphan_the_source(session, first_id)
        row = session.get(Resource, resource_id)
        row.status = "archived"
        session.commit()
        close_boundary(session)

        successor = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        result = scan_source(session, successor, settings)
        assert result.counters.rebound == 0
        assert any(warning.code == "rebind_skipped_archived" for warning in result.warnings)
        close_boundary(session)
        row = session.get(Resource, resource_id)
        assert row.source_id is None
        assert row.status == "archived"


def test_incomplete_scan_re_adopts_but_never_marks_missing(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "A.md").write_text("a", encoding="utf-8")
    (root / "B.md").write_text("b", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, first_id, settings)
        resource_id = resource_by_locator(session, context_id=context_id, relative_path="A.md").id
        close_boundary(session)
        _orphan_the_source(session, first_id)
        close_boundary(session)

        successor = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        result = scan_source(
            session, successor, settings, limits=DEFAULT_LIMITS.narrowed(max_entries=1)
        )
        assert result.complete is False
        assert result.counters.rebound == 1
        assert result.counters.marked_missing == 0
        close_boundary(session)
        # B.md was never enumerated, so it stays active instead of vanishing.
        assert session.get(Resource, resource_id).status == "active"
