"""Deterministic races against temporary databases and source files only."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from threading import Barrier, Event as ThreadEvent

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from organiser.api.resources import build_router
from organiser.db.models import Event, ProcessingRun, Resource, ResourceSnapshot
from organiser.db.session import create_session_factory
from organiser.services import source_scanner
from organiser.services.resources import scan as resources
from organiser.services.source_scanner import reading as scanner_reading
from organiser.services.resources import get_resource_snapshot, scan_source
from support.ingestion import (
    close_boundary,
    make_settings,
    seed_context,
    seed_source_id,
    session_for,
)


@pytest.mark.parametrize("overlapping_sources", [False, True])
def test_parallel_scans_unique_conflict_is_terminal_http_409(
    tmp_path, monkeypatch, overlapping_sources
):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "Notiz.md"
    target.write_text("gemeinsame Version", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        loser_source = seed_source_id(session, context_id, path=str(root))
        winner_source = (
            seed_source_id(session, context_id, path=str(target), source_type="document")
            if overlapping_sources
            else loser_source
        )
    if overlapping_sources:
        # Only the losing folder sees this earlier entry. Its staged insert and
        # registration event must roll back with the later shared-file conflict.
        (root / "A.bin").write_bytes(b"loser-only observation")

    # Both scans finish planning while the registry is still empty. Then the
    # winner commits before the loser applies its stale create plan. No sleep,
    # fabricated SQL exception or SQLite lock timing is involved.
    planned = Barrier(2)
    winner_finished = ThreadEvent()
    real_extract = resources._run_extractions

    real_plan = resources._plan_observations
    loser_states = set()

    def role_plan(session, *args, **kwargs):
        result = real_plan(session, *args, **kwargs)
        # Request-specific role is recorded by the test client dependency below.
        if session.info.get("loser"):
            loser_states.add(id(result[0]))
        return result

    def synchronized_extract(*args, **kwargs):
        planned.wait(timeout=15)
        if id(kwargs["state"]) in loser_states:
            assert winner_finished.wait(timeout=15)
        return real_extract(*args, **kwargs)

    monkeypatch.setattr(resources, "_plan_observations", role_plan)
    monkeypatch.setattr(resources, "_run_extractions", synchronized_extract)

    # Mount only B's router with explicit test authentication and temporary DB.
    factory = create_session_factory(settings)

    def request(source_id, winner=False):
        def get_session():
            with factory() as session:
                session.info["loser"] = not winner
                yield session

        def require_token(authorization: str = Header(default="")):
            if authorization != "Bearer race-test-token":
                raise HTTPException(status_code=401)

        app = FastAPI()
        app.include_router(
            build_router(get_session, settings), dependencies=[Depends(require_token)]
        )
        try:
            with TestClient(app) as client:
                return client.post(
                    f"/contexts/{context_id}/sources/{source_id}/scan",
                    headers={"Authorization": "Bearer race-test-token"},
                )
        finally:
            if winner:
                winner_finished.set()

    with (
        ThreadPoolExecutor(max_workers=1) as loser_pool,
        ThreadPoolExecutor(max_workers=1) as winner_pool,
    ):
        loser = loser_pool.submit(request, loser_source)
        winner = winner_pool.submit(request, winner_source, True)
        winner_response = winner.result(timeout=30)
        loser_response = loser.result(timeout=30)

    assert winner_response.status_code == 200
    assert loser_response.status_code == 409
    assert loser_response.json()["detail"]["code"] == "transaction_conflict"
    assert str(tmp_path) not in loser_response.text
    assert "INSERT" not in loser_response.text
    with session_for(settings) as session:
        rows = list(session.scalars(select(Resource)))
        assert len(rows) == 1
        assert rows[0].current_content_hash == hashlib.sha256(target.read_bytes()).hexdigest()
        assert len(list(session.scalars(select(ResourceSnapshot)))) == 1
        runs = list(session.scalars(select(ProcessingRun)))
        assert sorted(run.status for run in runs) == ["completed", "failed"]
        assert all(run.finished_at is not None for run in runs)
        failed = next(run for run in runs if run.status == "failed")
        assert failed.output_payload["error_code"] == "transaction_conflict"
        assert failed.output_payload["failure_class"] == "concurrent_conflict"
        assert failed.output_payload["phase"] == "result_transaction"
        events = list(session.scalars(select(Event)))
        assert sum(event.event_type == "RESOURCE_REGISTERED" for event in events) == 1
        assert sum(event.event_type == "CONTEXT_SOURCE_SCANNED" for event in events) == 1
        assert sum(event.event_type == "CONTEXT_SOURCE_REJECTED" for event in events) == 1
        close_boundary(session)
        # The session and registry remain usable after the loser rolled back.
        monkeypatch.undo()
        retry = scan_source(session, loser_source, settings)
        assert retry.complete is True
        assert retry.counters.created == int(overlapping_sources)
        assert len(list(session.scalars(select(Resource)))) == 1 + int(overlapping_sources)


@pytest.mark.parametrize("source_type", ["folder", "document", "note"])
@pytest.mark.parametrize("previous_version", [False, True])
@pytest.mark.parametrize("replace_at", ["read", "after_hash"])
def test_enumeration_replace_never_publishes_old_hash_or_marks_missing(
    tmp_path, monkeypatch, source_type, previous_version, replace_at
):
    root = tmp_path / "source"
    root.mkdir()
    target = root / "A.md"
    target.write_text("alte Version", encoding="utf-8")
    other = root / "Z.md"
    other.write_text("unbekannte Abwesenheit", encoding="utf-8")
    replacement = tmp_path / "replacement.md"
    replacement.write_text("neue Version", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(
            session,
            context_id,
            path=str(root if source_type == "folder" else target),
            source_type=source_type,
        )
        if previous_version:
            scan_source(session, source_id, settings)
            close_boundary(session)
        other.unlink()
        old_inode = target.stat().st_ino
        replaced = []

        def replace_once():
            if not replaced:
                os.replace(replacement, target)
                replaced.append(True)

        if replace_at == "after_hash":
            real_hash = source_scanner._hash_open_file

            def replacing_hash(*args, **kwargs):
                result = real_hash(*args, **kwargs)
                assert result[0] == hashlib.sha256(b"alte Version").hexdigest()
                replace_once()  # after both fstats; only the name check can catch it
                return result

            monkeypatch.setattr(scanner_reading, "_hash_open_file", replacing_hash)
        else:
            real_read = os.read

            def replacing_read(fd, size):
                data = real_read(fd, size)
                if os.fstat(fd).st_ino == old_inode:
                    replace_once()
                return data

            monkeypatch.setattr(os, "read", replacing_read)

        result = scan_source(session, source_id, settings)
        assert replaced == [True]
        assert target.stat().st_ino != old_inode
        assert result.complete is False
        assert result.status == "incomplete"
        assert result.error_code == "file_unstable"
        assert result.counters.marked_missing == 0
        assert "scan_incomplete_no_missing_decision" in {
            warning.code for warning in result.warnings
        }
        close_boundary(session)
        row = session.scalar(select(Resource).where(Resource.locator == str(target)))
        assert row.current_content_hash is None
        assert get_resource_snapshot(session, row.id).current_snapshot is None
        snapshots = list(session.scalars(select(ResourceSnapshot)))
        expected_snapshots = (2 if source_type == "folder" else 1) if previous_version else 0
        assert len(snapshots) == expected_snapshots
        assert all(resource.status == "active" for resource in session.scalars(select(Resource)))
        run = session.get(ProcessingRun, result.processing_run_id)
        assert run.status == "incomplete"
        assert run.finished_at is not None
        monkeypatch.undo()
        close_boundary(session)
        retry = scan_source(session, source_id, settings)
        assert retry.complete is True
        close_boundary(session)
        assert (
            session.get(Resource, row.id).current_content_hash
            == hashlib.sha256(target.read_bytes()).hexdigest()
        )
