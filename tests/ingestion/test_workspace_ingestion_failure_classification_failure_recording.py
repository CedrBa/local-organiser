"""Failure recording contracts for ingestion."""

from __future__ import annotations

import sqlite3
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from organiser.db.models import ProcessingRun, Resource
from organiser.domain.resources import ResourceIngestionError, SourceStaleError
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


@pytest.mark.parametrize("failed_commit", ["opening", "result", "terminal", "all"])
def test_commit_failure_keeps_original_cause_and_reports_failed_recording(
    course, monkeypatch, caplog, failed_commit
):
    settings, source_id = course
    with session_for(settings) as session:
        # Alembic's fixture logging config disables pre-existing module loggers.
        monkeypatch.setattr(resource_constants._SCAN_LOG, "disabled", False)
        monkeypatch.setattr(resource_constants._SCAN_LOG, "handlers", [caplog.handler])
        real_commit = session.commit
        primary = RuntimeError(SECRET)
        secondary = RuntimeError("SECONDARY " + SECRET)
        calls = []

        def commit():
            calls.append(True)
            count = len(calls)
            if (
                failed_commit == "all"
                or (failed_commit == "opening" and count == 1)
                or (failed_commit == "result" and count == 2)
            ):
                raise primary
            if failed_commit == "terminal" and count == 2:
                raise secondary
            return real_commit()

        if failed_commit == "terminal":

            def failing_enumeration(*args, **kwargs):
                raise primary

            monkeypatch.setattr(resources, "scan_source_tree", failing_enumeration)
        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert error.__cause__ is primary
        assert error.code == "unexpected_internal"
        assert error.phase == (
            "run_opening"
            if failed_commit in ("opening", "all")
            else "enumeration"
            if failed_commit == "terminal"
            else "result_commit"
        )
        assert error.terminalization_failed is (failed_commit in ("terminal", "all"))
        if error.terminalization_failed:
            assert "terminalization" in caplog.text
            assert "terminalization" in " ".join(error.__notes__)
            with session_for(settings) as observer:
                runs = list(observer.scalars(select(ProcessingRun)))
                assert [run.status for run in runs] == (
                    ["running"] if failed_commit == "terminal" else []
                )
        else:
            assert_failed(session, error, "unexpected_internal", error.phase)
            assert list(session.scalars(select(Resource))) == []
        assert_safe(session, error, settings, caplog)


def test_run_opening_real_integrity_failure_is_safe_and_traceable(course, caplog):
    settings, source_id = course
    with session_for(settings) as session:
        session.execute(
            text(
                "CREATE TRIGGER reject_run BEFORE INSERT ON processing_runs BEGIN SELECT RAISE(ABORT, 'SELECT private FROM /tmp/private/source.md'); END"
            )
        )
        session.commit()
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert isinstance(error.__cause__, IntegrityError)
        assert error.__cause__.orig.sqlite_errorcode == sqlite3.SQLITE_CONSTRAINT_TRIGGER
        assert error.code == "persistence_integrity"
        assert error.failure_class == "persistence_integrity"
        assert error.phase == "run_opening"
        assert error.terminalization_failed is True
        assert list(session.scalars(select(ProcessingRun))) == []
        assert_safe(session, error, settings, caplog)


def test_opening_commit_acknowledgement_failure_reuses_run_identity(course, monkeypatch, caplog):
    settings, source_id = course
    with session_for(settings) as session:
        real_commit = session.commit
        calls = []
        primary = RuntimeError(SECRET)

        def commit():
            real_commit()
            calls.append(True)
            if len(calls) == 1:
                raise primary

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        assert raised.value.__cause__ is primary
        assert_failed(session, raised.value, "unexpected_internal", "run_opening")
        assert_safe(session, raised.value, settings, caplog)


def test_real_run_update_constraint_preserves_primary_and_exposes_recording_failure(
    course, monkeypatch, caplog
):
    settings, source_id = course
    with session_for(settings) as session:
        session.execute(
            text(
                "CREATE TRIGGER reject_run_update BEFORE UPDATE ON processing_runs BEGIN SELECT RAISE(ABORT, 'SELECT private FROM /tmp/private/source.md'); END"
            )
        )
        session.commit()
        monkeypatch.setattr(resource_constants._SCAN_LOG, "disabled", False)
        monkeypatch.setattr(resource_constants._SCAN_LOG, "handlers", [caplog.handler])
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert error.code == "persistence_integrity"
        assert error.failure_class == "persistence_integrity"
        assert error.phase == "result_commit"
        assert isinstance(error.__cause__, IntegrityError)
        assert error.terminalization_failed is True
        assert "terminalization" in caplog.text
        assert list(session.scalars(select(Resource))) == []
        run = session.scalar(select(ProcessingRun))
        assert run.status == "running"  # database rejects every status update
        assert_safe(session, error, settings, caplog)


@pytest.mark.parametrize("broken_logging", [False, True])
def test_domain_primary_survives_terminalizer_failure(course, monkeypatch, caplog, broken_logging):
    settings, source_id = course
    primary = SourceStaleError("Sichere Ablehnung", reason="source_removed")

    def stale(*args, **kwargs):
        raise primary

    def cannot_finish(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(resources, "scan_source_tree", stale)
    if broken_logging:
        monkeypatch.setattr(resource_constants._SCAN_LOG, "error", cannot_finish)
    with session_for(settings) as session:
        real_commit = session.commit
        commits = []

        def commit():
            commits.append(True)
            if len(commits) == 1:
                return real_commit()
            return cannot_finish()

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        assert raised.value is primary
        assert (primary.status_code, primary.code) == (409, "source_stale")
        assert primary.failure_class == "expected_refusal"
        assert primary.terminalization_failed is True
        assert "terminalization" in " ".join(primary.__notes__)
        if broken_logging:
            assert "scan diagnostic logging failed" in primary.__notes__
        assert_safe(session, primary, settings, caplog)


def test_broken_success_payload_cannot_break_independent_terminalization(
    course, monkeypatch, caplog
):
    settings, source_id = course
    real_finish = resources._finish_run

    def corrupt_finish(session, run, **kwargs):
        run.output_payload = [SECRET]
        real_finish(session, run, **kwargs)

    monkeypatch.setattr(resources, "_finish_run", corrupt_finish)
    with session_for(settings) as session:
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert isinstance(error.__cause__, TypeError)
        assert_failed(session, error, "unexpected_internal", "run_completion")
        assert list(session.scalars(select(Resource))) == []
        assert_safe(session, error, settings, caplog)
