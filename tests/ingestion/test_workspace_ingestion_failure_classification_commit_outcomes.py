"""Commit outcomes contracts for ingestion."""

from __future__ import annotations

import sqlite3
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from organiser.db.models import Event, ProcessingRun, Resource, ResourceSnapshot
from organiser.domain.resources import DEFAULT_LIMITS, ResourceIngestionError
from organiser.services.resources import scan as resources
from organiser.services.resources import constants as resource_constants
from support.ingestion import session_for
from support.workspace_ingestion_failure_classification import (
    SECRET as SECRET,
    PHASES as PHASES,
    course as course,
    assert_safe as assert_safe,
    assert_failed as assert_failed,
    assert_committed_result as assert_committed_result,
)


@pytest.mark.parametrize("hook", ["summarize_warnings", "ScanResult"])
@pytest.mark.parametrize("incomplete", [False, True])
def test_response_failure_preserves_committed_atomic_result(
    course, monkeypatch, caplog, hook, incomplete
):
    settings, source_id = course
    if incomplete:
        (settings.database_path.parent / "source" / "Z.md").write_text(
            "weitere Datei", encoding="utf-8"
        )
    limits = DEFAULT_LIMITS.narrowed(max_entries=1) if incomplete else DEFAULT_LIMITS
    primary = RuntimeError(SECRET)

    def failed_response(*args, **kwargs):
        raise primary

    monkeypatch.setattr(resources, hook, failed_response)
    with session_for(settings) as session:
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings, limits=limits)
        error = raised.value
        assert error.__cause__ is primary
        assert (error.code, error.status_code, error.failure_class) == (
            "unexpected_internal",
            500,
            "unexpected_internal",
        )
        assert error.phase == "response"
        assert error.context["commit_outcome"] == "committed"
        assert error.context["retry_safe"] is False
        assert error.terminalization_failed is False
        assert_committed_result(session, error, incomplete=incomplete)
        assert_safe(session, error, settings, caplog)


@pytest.mark.parametrize("incomplete", [False, True])
@pytest.mark.parametrize("integrity", [False, True])
def test_result_commit_acknowledgement_loss_preserves_success(
    course, monkeypatch, caplog, incomplete, integrity
):
    settings, source_id = course
    if incomplete:
        (settings.database_path.parent / "source" / "Z.md").write_text(
            "weitere Datei", encoding="utf-8"
        )
    limits = DEFAULT_LIMITS.narrowed(max_entries=1) if incomplete else DEFAULT_LIMITS
    # Even a known-looking UNIQUE exception after a proven successful commit
    # must not produce a retryable transaction_conflict or a failed run.
    if integrity:
        original = sqlite3.IntegrityError(
            "UNIQUE constraint failed: resources.context_id, resources.locator"
        )
        original.sqlite_errorcode = sqlite3.SQLITE_CONSTRAINT_UNIQUE
        primary = IntegrityError(SECRET, {"path": SECRET}, original)
    else:
        primary = RuntimeError(SECRET)
    with session_for(settings) as session:
        real_commit = session.commit
        calls = []

        def commit():
            real_commit()
            calls.append(True)
            if len(calls) == 2:
                raise primary

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings, limits=limits)
        error = raised.value
        assert error.__cause__ is primary
        expected = "persistence_integrity" if integrity else "unexpected_internal"
        assert (error.failure_class, error.code, error.status_code) == (expected, expected, 500)
        assert error.phase == "result_commit"
        assert error.context["commit_outcome"] == "committed"
        assert error.context["retry_safe"] is False
        assert_committed_result(session, error, incomplete=incomplete)
        assert_safe(session, error, settings, caplog)


@pytest.mark.parametrize("actually_committed", [False, True])
def test_unverifiable_commit_outcome_is_explicit_and_never_overwrites_run(
    course, monkeypatch, caplog, actually_committed
):
    settings, source_id = course
    primary = RuntimeError(SECRET)
    with session_for(settings) as session:
        real_commit = session.commit
        real_get_bind = session.get_bind
        calls = []
        readback_blocked = []

        def get_bind(*args, **kwargs):
            if readback_blocked:
                raise RuntimeError("READBACK " + SECRET)
            return real_get_bind(*args, **kwargs)

        def commit():
            calls.append(True)
            if len(calls) == 2:
                if actually_committed:
                    real_commit()
                readback_blocked.append(True)
                raise primary
            return real_commit()

        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(session, "get_bind", get_bind)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert error.__cause__ is primary
        assert error.code == "unexpected_internal"
        assert error.context["commit_outcome"] == "unknown"
        assert error.context["retry_safe"] is False
        assert error.terminalization_skipped is True
        assert len(calls) == 2  # no blind result retry or failure write
        with session_for(settings) as observer:
            if actually_committed:
                assert_committed_result(observer, error, diagnostics=False)
            else:
                run = observer.scalar(select(ProcessingRun))
                assert run.id == error.processing_run_id
                assert run.status == "running"
                assert list(observer.scalars(select(Resource))) == []
                assert list(observer.scalars(select(ResourceSnapshot))) == []
                assert not any(
                    event.event_type
                    in {resources.EVENT_SOURCE_SCANNED, resource_constants.EVENT_SOURCE_REJECTED}
                    for event in observer.scalars(select(Event))
                )
            assert_safe(observer, error, settings, caplog)


def test_post_commit_diagnostic_failure_does_not_change_success_or_primary(
    course, monkeypatch, caplog
):
    settings, source_id = course
    primary = RuntimeError(SECRET)

    def failed_response(*args, **kwargs):
        raise primary

    monkeypatch.setattr(resources, "summarize_warnings", failed_response)
    with session_for(settings) as session:
        real_commit = session.commit
        calls = []

        def commit():
            calls.append(True)
            if len(calls) == 3:
                raise RuntimeError("DIAGNOSTIC " + SECRET)
            real_commit()

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert error.__cause__ is primary
        assert error.diagnostic_recording_failed is True
        assert error.terminalization_failed is False
        assert_committed_result(session, error, diagnostics=False)
        assert_safe(session, error, settings, caplog)
