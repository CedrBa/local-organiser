"""Scan limits contracts for ingestion."""

from __future__ import annotations

from sqlalchemy import select
from organiser.db.models import Event, Resource
from organiser.domain.resources import (
    DEFAULT_LIMITS,
)
from organiser.services.resources import scan_source
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
    snapshots_of,
)


def test_extraction_budget_bounds_the_documents_read_per_scan(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(4):
        (root / f"Notiz{index}.md").write_text(f"text {index}", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_extractions=2)
        )
        assert result.counters.created == 4
        close_boundary(session)
        ready = [
            snapshot
            for resource in session.scalars(select(Resource))
            for snapshot in snapshots_of(session, resource.id)
        ]
        assert len(ready) == 2
        assert any(warning.code == "extraction_budget_exhausted" for warning in result.warnings)


# -- repeated scans, changes, moves, missing ---------------------------------


def test_repeated_scan_changes_nothing_and_creates_no_duplicate_snapshot(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("stable", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        first = scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        second = scan_source(session, source_id, settings)
        assert first.counters.created == 1
        assert second.counters.created == 0
        assert second.counters.unchanged == 1
        assert len(snapshots_of(session, resource_id)) == 1
        registrations = list(
            session.scalars(
                select(Event).where(
                    Event.entity_id == resource_id, Event.event_type == "RESOURCE_REGISTERED"
                )
            )
        )
        assert len(registrations) == 1


def test_changed_file_gets_a_new_version_and_keeps_the_old_snapshot(tmp_path):
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
        first_hash = session.get(Resource, resource_id).current_content_hash
        close_boundary(session)
        target.write_text("zweite Fassung", encoding="utf-8")
        second = scan_source(session, source_id, settings)
        assert second.counters.refreshed == 1
        assert session.get(Resource, resource_id).current_content_hash != first_hash
        assert len(snapshots_of(session, resource_id)) == 2


def test_rename_creates_a_new_resource_and_marks_the_old_one_missing(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    old = root / "Notiz.md"
    old.write_text("inhalt", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        first_id = resource_by_locator(session, context_id=context_id, relative_path="Notiz.md").id
        close_boundary(session)
        old.rename(root / "Notiz2.md")
        result = scan_source(session, source_id, settings)
        assert result.counters.created == 1
        assert result.counters.marked_missing == 1
        assert session.get(Resource, first_id).status == "missing"
        second_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz2.md"
        ).id
        assert second_id != first_id
        assert session.get(Resource, second_id).status == "active"
        assert len(snapshots_of(session, first_id)) == 1


def test_incomplete_scan_never_marks_anything_missing(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "A.md").write_text("a", encoding="utf-8")
    (root / "B.md").write_text("b", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        victim_id = resource_by_locator(session, context_id=context_id, relative_path="B.md").id
        close_boundary(session)
        (root / "B.md").unlink()
        (root / "C.md").write_text("c", encoding="utf-8")
        result = scan_source(
            session, source_id, settings, limits=DEFAULT_LIMITS.narrowed(max_entries=1)
        )
        assert result.complete is False
        assert result.counters.marked_missing == 0
        assert session.get(Resource, victim_id).status == "active"


def test_failed_extraction_does_not_make_a_file_missing(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Uebung.md").write_text("uebung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Uebung.md"
        ).id
        snapshot = snapshots_of(session, resource_id)[0]
        snapshot.status = "failed"
        snapshot.text = None
        session.commit()
        result = scan_source(session, source_id, settings)
        assert result.complete is True
        assert result.counters.marked_missing == 0
        assert session.get(Resource, resource_id).status == "active"
        assert len(snapshots_of(session, resource_id)) == 1
