"""Shared fixtures and builders for test_workspace_ingestion_failure_classification contracts; no executable tests."""

from __future__ import annotations

import json
import pytest
from sqlalchemy import select
from organiser.db.models import Event, ProcessingRun, Resource, ResourceSnapshot
from organiser.services.resources import scan as resources
from organiser.services.resources import constants as resource_constants
from support.ingestion import make_settings, seed_context, seed_source_id, session_for

SECRET = "SELECT private FROM /tmp/private/source.md"

PHASES = [
    ("scan_source_tree", "enumeration"),
    ("_plan_observations", "planning"),
    ("_run_extractions", "extraction"),
    ("_apply_observations", "result_transaction"),
    ("_finish_run", "run_completion"),
    ("_audit", "audit"),
]


@pytest.fixture
def course(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "Notiz.md").write_text("Version", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
    return settings, source_id


def assert_safe(session, error, settings, caplog):
    payloads = [
        error.as_payload(),
        caplog.text,
        getattr(error, "__notes__", []),
        [
            {"payload": run.output_payload, "error": run.error}
            for run in session.scalars(select(ProcessingRun))
        ],
        [
            event.payload
            for event in session.scalars(select(Event))
            # Seed-domain events belong to the existing source-registration service;
            # inspect every event emitted by this scan/failure boundary.
            if event.event_type
            in {
                resources.EVENT_SOURCE_SCANNED,
                resource_constants.EVENT_SOURCE_REJECTED,
                resource_constants.EVENT_RESOURCE_REGISTERED,
                resource_constants.EVENT_RESOURCE_REFRESHED,
                resource_constants.EVENT_RESOURCE_MISSING,
                resource_constants.EVENT_RESOURCE_EXTRACTED,
            }
        ],
    ]
    encoded = json.dumps(payloads)
    assert SECRET not in encoded
    assert "SELECT private" not in encoded
    assert str(settings.database_path.parent) not in encoded


def assert_failed(session, error, failure_class, phase):
    runs = list(session.scalars(select(ProcessingRun)))
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "failed"
    assert run.finished_at is not None
    assert run.output_payload["failure_class"] == failure_class
    assert run.output_payload["phase"] == phase
    assert run.output_payload["error_code"] == error.code
    assert run.error == error.code
    assert error.terminalization_failed is False
    return run


def assert_committed_result(session, error, *, incomplete=False, diagnostics=True):
    """Check the whole atomic result, not merely a terminal run label."""
    runs = list(session.scalars(select(ProcessingRun)))
    assert len(runs) == 1
    run = runs[0]
    assert run.id == error.processing_run_id
    assert run.status == ("incomplete" if incomplete else "completed")
    assert run.finished_at is not None
    assert run.error == ("entry_limit_reached" if incomplete else None)
    assert run.output_payload["complete"] is not incomplete
    assert run.output_payload["counters"]["created"] == 1
    assert "failure_class" not in run.output_payload
    if diagnostics:
        assert run.output_payload["delivery_failure"] == {
            "failure_class": error.failure_class,
            "error_code": error.code,
            "phase": error.phase,
            "commit_outcome": "committed",
        }
    else:
        assert "delivery_failure" not in run.output_payload
    rows = list(session.scalars(select(Resource)))
    snapshots = list(session.scalars(select(ResourceSnapshot)))
    assert len(rows) == len(snapshots) == 1
    assert rows[0].status == "active"
    assert rows[0].current_content_hash == snapshots[0].content_hash
    assert snapshots[0].resource_id == rows[0].id
    assert snapshots[0].status == "ready"
    events = list(session.scalars(select(Event)))
    assert (
        sum(event.event_type == resource_constants.EVENT_RESOURCE_REGISTERED for event in events)
        == 1
    )
    assert (
        sum(event.event_type == resource_constants.EVENT_RESOURCE_EXTRACTED for event in events)
        == 1
    )
    scan_events = [event for event in events if event.event_type == resources.EVENT_SOURCE_SCANNED]
    assert len(scan_events) == 1
    assert scan_events[0].payload["run_id"] == run.id
    assert scan_events[0].payload["complete"] is not incomplete
    assert not any(event.event_type == resource_constants.EVENT_SOURCE_REJECTED for event in events)
