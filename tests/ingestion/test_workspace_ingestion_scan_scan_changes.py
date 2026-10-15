"""Scan changes contracts for ingestion."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from organiser.db.models import Event, ProcessingRun, Resource, ResourceSnapshot, Task, Topic
from organiser.domain.resources import (
    ResourceIngestionError,
)
from organiser.services.resources import list_resources, scan_source
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
)


def test_file_reappearing_becomes_active_again(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("da", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        target.unlink()
        scan_source(session, source_id, settings)
        assert session.get(Resource, resource_id).status == "missing"
        close_boundary(session)
        target.write_text("wieder da", encoding="utf-8")
        scan_source(session, source_id, settings)
        assert session.get(Resource, resource_id).status == "active"


# -- several sources, overlap, contexts --------------------------------------


def test_overlapping_sources_keep_the_first_reference(tmp_path):
    first = tmp_path / "erster"
    second = tmp_path / "zweiter"
    first.mkdir()
    second.mkdir()
    (first / "eigen.md").write_text("eigen", encoding="utf-8")
    (second / "eigen.md").write_text("eigen", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(session, context_id, path=str(first))
        second_id = seed_source_id(session, context_id, path=str(second))
        scan_source(session, first_id, settings)
        result = scan_source(session, second_id, settings)
        assert result.counters.created == 1
        assert result.counters.duplicates == 0
        # Two different addresses, so nothing is missing and nothing is shared.
        assert result.counters.marked_missing == 0
        resources = {
            resource.locator: resource.source_id for resource in session.scalars(select(Resource))
        }
        assert resources == {str(first / "eigen.md"): first_id, str(second / "eigen.md"): second_id}


def test_same_file_in_two_sources_of_one_context_stays_two_resources(tmp_path):
    first = tmp_path / "kurs1"
    second = tmp_path / "kurs2"
    first.mkdir()
    second.mkdir()
    (first / "Notiz.md").write_text("a", encoding="utf-8")
    (second / "Notiz.md").write_text("a", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        first_id = seed_source_id(session, context_id, path=str(first))
        second_id = seed_source_id(session, context_id, path=str(second))
        scan_source(session, first_id, settings)
        result = scan_source(session, second_id, settings)
        assert result.counters.created == 1
        assert result.counters.marked_missing == 0
        resources = list(session.scalars(select(Resource).order_by(Resource.locator)))
        assert len(resources) == 2
        assert {resource.source_id for resource in resources} == {first_id, second_id}


def test_identical_titles_in_two_contexts_stay_separate(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        first_context = seed_context(session, title="Finanzierung").id
        close_boundary(session)
        second_context = seed_context(session, title="Finanzierung").id
        first_source = seed_source_id(session, first_context, path=str(root))
        close_boundary(session)
        second_source = seed_source_id(session, second_context, path=str(root))
        close_boundary(session)
        scan_source(session, first_source, settings)
        scan_source(session, second_source, settings)
        first_page = list_resources(session, first_context)
        second_page = list_resources(session, second_context)
        assert first_page.total == 1 and second_page.total == 1
        assert first_page.items[0].id != second_page.items[0].id
        assert first_page.items[0].context_id == first_context


def test_list_resources_is_bounded_and_deterministic(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    for index in range(5):
        (root / f"Notiz{index}.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        close_boundary(session)
        page = list_resources(session, context_id, limit=2)
        assert page.total == 5
        assert len(page.items) == 2
        assert list_resources(session, context_id, limit=2).items == page.items
        assert list_resources(session, context_id, limit=2, offset=4).items[0] != page.items[0]
        assert list_resources(session, context_id, limit=10_000).limit == 200
        assert list_resources(session, context_id, status="missing").total == 0


def test_unknown_context_is_not_an_empty_inventory(tmp_path):
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        from organiser.domain.resources import ContextNotFoundError

        with pytest.raises(ContextNotFoundError):
            list_resources(session, "gibt-es-nicht")


# -- audit and side effects --------------------------------------------------


def test_scan_is_audited_with_a_processing_run_and_events(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        result = scan_source(session, source_id, settings)
        run = session.get(ProcessingRun, result.processing_run_id)
        assert run.run_type == "resource_scan"
        assert run.status == "completed"
        assert run.output_payload["complete"] is True
        assert run.output_payload["context_id"] == context_id
        ingestion_events = [
            event
            for event in session.scalars(select(Event))
            if event.event_type.startswith("RESOURCE_")
            or event.event_type == "CONTEXT_SOURCE_SCANNED"
        ]
        assert {event.event_type for event in ingestion_events} == {
            "RESOURCE_REGISTERED",
            "RESOURCE_EXTRACTED",
            "CONTEXT_SOURCE_SCANNED",
        }
        for event in ingestion_events:
            assert str(root) not in str(event.payload)


def test_rejected_scan_still_records_a_failed_run(tmp_path):
    settings = make_settings(tmp_path)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(tmp_path / "kurs"))
        with pytest.raises(ResourceIngestionError):
            scan_source(session, source_id, settings)
        runs = list(session.scalars(select(ProcessingRun)))
        assert len(runs) == 1
        assert runs[0].status == "failed"
        assert runs[0].output_payload["error_code"] == "source_not_allowed"
        assert list(session.scalars(select(ResourceSnapshot))) == []


def test_scan_creates_no_topics_tasks_or_files(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        scan_source(session, source_id, settings)
        assert list(session.scalars(select(Topic))) == []
        assert list(session.scalars(select(Task))) == []
        assert sorted(path.name for path in root.iterdir()) == ["Notiz.md"]
