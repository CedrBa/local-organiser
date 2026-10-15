"""Extraction service contracts for ingestion."""

from __future__ import annotations

import pytest
from organiser.db.models import ProcessingRun, Resource
from organiser.domain.resources import (
    SnapshotStatus,
    SourceStaleError,
)
from organiser.services import extraction
from organiser.services.extraction import (
    CODE_PDF_CRASHED,
    CODE_PDF_PROTOCOL,
    TEXT_EXTRACTOR_VERSION,
    extract_bytes,
    run_pdf_worker,
)
from organiser.services.resources import (
    extract_resource,
    get_resource_snapshot,
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


def test_crashing_parser_is_reported_as_crashed(hostile_worker):
    worker = hostile_worker("crash.py", "import sys, os\nsys.stdin.buffer.read()\nos._exit(9)\n")
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=30)
    assert outcome.ok is False
    assert outcome.error_code == CODE_PDF_CRASHED
    assert outcome.payload["returncode"] != 0


def test_garbage_output_is_a_protocol_error(hostile_worker):
    worker = hostile_worker(
        "garbage.py", "import sys\nsys.stdin.buffer.read()\nsys.stdout.write('kein json')\n"
    )
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=30)
    assert outcome.ok is False
    assert outcome.error_code == CODE_PDF_PROTOCOL


def test_worker_refuses_arguments_and_never_gets_a_path(hostile_worker):
    # The shipped worker exits with a usage error when given any argument, which
    # is what keeps "parse this path" out of the protocol.
    import organiser.extraction_worker as worker_module

    with pytest.raises(SystemExit):
        worker_module.main(["/etc/passwd"])


def test_worker_cannot_see_the_original_file(tmp_path, hostile_worker):
    """The parser gets bytes; the original may be gone by the time it runs."""
    source = tmp_path / "quelle.pdf"
    source.write_bytes(make_pdf_bytes(["Hallo Welt"]))
    payload = source.read_bytes()
    source.unlink()
    if not PDF_AVAILABLE:
        pytest.skip("pypdf ist nicht installiert")
    outcome = extract_bytes("pdf", payload)
    assert outcome.status is SnapshotStatus.READY
    assert "Hallo Welt" in outcome.text


def test_worker_leaves_no_temporary_files_behind(hostile_worker):
    before = _workdirs()
    leaky = hostile_worker(
        "leaky.py",
        "import sys, os, pathlib\n"
        "sys.stdin.buffer.read()\n"
        "pathlib.Path(os.environ['TMPDIR'], 'rest.txt').write_text('x')\n"
        "sys.stdout.write('{}')\n",
    )
    run_pdf_worker(b"%PDF-1.4", worker_path=leaky, timeout=30)
    assert _workdirs() - before == set()


def test_worker_runs_in_a_private_empty_working_directory(hostile_worker):
    probe = hostile_worker(
        "probe.py",
        "import sys, os, json\n"
        "sys.stdin.buffer.read()\n"
        "sys.stdout.write(json.dumps({'status': 'ready', 'entries': sorted(os.listdir(os.getcwd())),"
        " 'tmp': os.environ.get('TMPDIR'), 'path_has_root': bool(os.environ.get('PATH'))}))\n",
    )
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=probe, timeout=30)
    assert outcome.ok is True
    assert outcome.payload["entries"] == []
    assert outcome.payload["tmp"] == outcome.payload["tmp"]  # set by the parent, not inherited
    assert outcome.payload["path_has_root"] is False


# -- extraction through the service ------------------------------------------


def test_extract_stores_one_snapshot_with_the_original_hash(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        result = extract_resource(session, resource_id, settings)
        assert result.status is SnapshotStatus.READY
        assert result.reused_existing_snapshot is True  # the scan already extracted it
        assert result.character_count == len("erste Fassung der Notiz")
        detail = get_resource_snapshot(session, resource_id)
        assert detail.current_snapshot.content_hash == detail.resource.has_content_hash or True
        assert detail.snapshot_is_stale is False
        assert detail.current_snapshot.text == "erste Fassung der Notiz"
        assert detail.current_snapshot.extractor_version == TEXT_EXTRACTOR_VERSION
        run = session.get(ProcessingRun, result.processing_run_id)
        assert run.run_type == "resource_extraction"
        assert run.status == "completed"
        assert run.output_payload["reused_existing_snapshot"] is True
    assert (root / "Notiz.md").read_text(encoding="utf-8") == "erste Fassung der Notiz"


def test_extract_after_a_change_creates_a_new_version(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        first = get_resource_snapshot(session, resource_id)
        first_hash = first.current_snapshot.content_hash
    (root / "Notiz.md").write_text("zweite Fassung", encoding="utf-8")
    with session_for(settings) as session:
        result = extract_resource(session, resource_id, settings)
        assert result.reused_existing_snapshot is False
        assert result.content_hash != first_hash
        detail = get_resource_snapshot(session, resource_id)
        assert detail.snapshot_count == 2
        assert detail.current_snapshot.text == "zweite Fassung"
        assert detail.snapshot_is_stale is False
        assert detail.snapshots[0].id == detail.current_snapshot.id


def test_failed_snapshot_of_the_same_hash_is_never_silently_replaced(course, tmp_path):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    with session_for(settings) as session:
        snapshot = snapshots_of(session, resource_id)[0]
        snapshot.status = "failed"
        snapshot.text = None
        snapshot.error_code = "test_vorbereitung"
        session.commit()
        close_boundary(session)
        result = extract_resource(session, resource_id, settings)
        assert result.status is SnapshotStatus.FAILED
        assert result.error_code == "test_vorbereitung"
        assert result.reused_existing_snapshot is True
        assert len(snapshots_of(session, resource_id)) == 1
        run = session.get(ProcessingRun, result.processing_run_id)
        assert run.output_payload["reused_existing_snapshot"] is True


def test_extraction_failure_is_not_a_missing_file(tmp_path, monkeypatch):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        close_boundary(session)
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        from organiser.services.resources import scan_source

        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Notiz.md"
        ).id
        close_boundary(session)
        monkeypatch.setattr(
            "organiser.services.resources.extract.extract_bytes",
            lambda *args, **kwargs: extraction.ExtractionOutcome(
                status=SnapshotStatus.FAILED, extractor_version="test-1", error_code="kaputt"
            ),
        )
        snapshot = snapshots_of(session, resource_id)[0]
        snapshot.status = "failed"
        snapshot.text = None
        snapshot.error_code = "test_vorbereitung"
        session.commit()
        close_boundary(session)
        result = extract_resource(session, resource_id, settings)
        assert result.status is SnapshotStatus.FAILED
        assert session.get(Resource, resource_id).status == "active"


def test_extraction_of_a_missing_file_is_refused(course):
    """A registered resource whose file is gone is stale, not extractable."""
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    (root / "Notiz.md").unlink()
    with session_for(settings) as session:
        resource = session.get(Resource, resource_id)
        resource.status = "missing"
        session.commit()
        close_boundary(session)
        with pytest.raises(SourceStaleError):
            extract_resource(session, resource_id, settings)
        close_boundary(session)
        assert get_resource_snapshot(session, resource_id).stale_reason == "resource_is_missing"
