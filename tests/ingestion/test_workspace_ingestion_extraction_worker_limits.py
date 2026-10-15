"""Worker limits contracts for ingestion."""

from __future__ import annotations

import glob
import tempfile
import time
import pytest
from sqlalchemy import select
from organiser.db.models import ProcessingRun, ResourceSnapshot
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    SnapshotStatus,
)
from organiser.services import extraction
from organiser.services.extraction import (
    CODE_PDF_PROTOCOL,
    CODE_PDF_TIMEOUT,
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
)
from support.workspace_ingestion_extraction import (
    PDF_AVAILABLE as PDF_AVAILABLE,
    requires_pypdf as requires_pypdf,
    course as course,
    scan_and_get_resource_id as scan_and_get_resource_id,
    hostile_worker as hostile_worker,
    _workdirs as _workdirs,
)


def test_no_network_or_shell_is_used_by_the_ingestion_path(tmp_path, monkeypatch):
    """A scan must not reach the network or spawn a shell."""
    import socket
    import subprocess

    def forbidden(*args, **kwargs):  # pragma: no cover - only runs on a violation
        raise AssertionError("Netzwerk oder Shell im Aufnahmepfad")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
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
        extract_resource(session, resource_id, settings)


def test_tempfile_module_is_not_used_for_source_reads(course):
    settings, context_id, source_id, root = course
    resource_id = scan_and_get_resource_id(settings, context_id, source_id)
    before = set(glob.glob(str(root.parent / "*")))
    with session_for(settings) as session:
        extract_resource(session, resource_id, settings)
    assert set(glob.glob(str(root.parent / "*"))) == before
    assert tempfile.gettempdir()  # the module is only used for the parser sandbox


# -- regressions for the independent review ----------------------------------


def test_the_test_seam_cannot_raise_the_fixed_parser_budget(tmp_path, monkeypatch):
    """A caller may shorten the 20 s budget, never extend it."""
    worker = tmp_path / "hang.py"
    worker.write_text(
        "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(600)\n", encoding="utf-8"
    )
    started = time.monotonic()
    outcome = run_pdf_worker(
        b"%PDF-1.4",
        worker_path=worker,
        timeout=3600,
        limits=DEFAULT_LIMITS.narrowed(pdf_worker_timeout_seconds=0.4),
    )
    assert outcome.error_code == CODE_PDF_TIMEOUT
    assert time.monotonic() - started < 30


def test_a_cancelled_wait_still_kills_the_parser(tmp_path, monkeypatch):
    worker = tmp_path / "hang.py"
    worker.write_text(
        "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(600)\n", encoding="utf-8"
    )

    def interrupted(process, timeout, overflow):
        raise KeyboardInterrupt

    monkeypatch.setattr(extraction, "_wait_bounded", interrupted)
    with pytest.raises(KeyboardInterrupt):
        run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=30)
    # The parser is gone, and nothing of the sandbox survived.
    assert _workdirs() == set()


def test_parser_chatter_does_not_fake_an_output_limit(tmp_path):
    """More than 64 KiB of stderr is not an output-limit violation of the result."""
    worker = tmp_path / "chatty.py"
    worker.write_text(
        "import sys, json\n"
        "sys.stdin.buffer.read()\n"
        "sys.stderr.write('warnung\\n' * 20000)\n"
        "sys.stderr.flush()\n"
        "sys.stdout.write(json.dumps({'status': 'ready', 'text': 'ok'}))\n",
        encoding="utf-8",
    )
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=30)
    assert outcome.ok is True
    assert outcome.payload["text"] == "ok"


@requires_pypdf
def test_snapshot_metadata_records_the_parser_limits_it_actually_applied(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Vorlesung.pdf").write_bytes(make_pdf_bytes(["Bilanzierung"]))
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        close_boundary(session)
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        from organiser.services.resources import scan_source

        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path="Vorlesung.pdf"
        ).id
        close_boundary(session)
        detail = get_resource_snapshot(session, resource_id)
        applied = detail.current_snapshot.metadata_payload["limits_applied"]
        assert applied["cpu_seconds"] == DEFAULT_LIMITS.pdf_worker_cpu_seconds
        assert applied["address_space_bytes"] == DEFAULT_LIMITS.pdf_worker_address_space_bytes


@requires_pypdf
@pytest.mark.parametrize("remaining", [0, 1, 2])
def test_pdf_page_separator_respects_remaining_character_budget(remaining):
    first_page = "A" * (100 - remaining)
    outcome = extract_bytes(
        "pdf",
        make_pdf_bytes([first_page, "Second page"]),
        limits=DEFAULT_LIMITS.narrowed(max_text_characters=100),
    )
    assert outcome.status is SnapshotStatus.READY
    assert outcome.truncated is True
    assert outcome.character_count <= 100
    assert outcome.text == first_page + ("\n\n" if remaining == 2 else "")
    assert outcome.metadata["pages"] == [{"page": 1, "char_start": 0, "char_end": len(first_page)}]


@pytest.mark.parametrize("length", [100, 101])
def test_parent_checks_worker_text_character_limit(monkeypatch, length):
    monkeypatch.setattr(extraction, "pypdf_version", lambda: "review-test")
    monkeypatch.setattr(
        extraction,
        "run_pdf_worker",
        lambda *args, **kwargs: extraction.PdfWorkerOutcome(
            ok=True, payload={"status": "ready", "text": "x" * length}
        ),
    )
    outcome = extraction.extract_pdf_bytes(
        b"pdf bytes", limits=DEFAULT_LIMITS.narrowed(max_text_characters=100)
    )
    if length == 100:
        assert outcome.status is SnapshotStatus.READY and outcome.character_count == 100
    else:
        assert outcome.status is SnapshotStatus.FAILED
        assert outcome.error_code == CODE_PDF_PROTOCOL
        assert outcome.text is None


@requires_pypdf
@pytest.mark.parametrize("remaining", [0, 1, 2])
def test_pdf_page_boundary_full_scan_commits_bounded_snapshot(course, remaining):
    from organiser.services.resources import scan_source

    settings, context_id, source_id, root = course
    payload = make_pdf_bytes(
        ["A" * (DEFAULT_LIMITS.max_text_characters - remaining), "Second page"]
    )
    pdf = root / "Boundary.pdf"
    pdf.write_bytes(payload)
    with session_for(settings) as session:
        result = scan_source(session, source_id, settings)
        assert result.status == "completed"
        resource = resource_by_locator(session, context_id=context_id, relative_path="Boundary.pdf")
        snapshot = session.scalar(
            select(ResourceSnapshot).where(ResourceSnapshot.resource_id == resource.id)
        )
        assert snapshot.status == "ready" and snapshot.truncated
        assert len(snapshot.text) <= DEFAULT_LIMITS.max_text_characters
        assert session.get(ProcessingRun, result.processing_run_id).status == "completed"
        assert (
            resource_by_locator(session, context_id=context_id, relative_path="Notiz.md")
            is not None
        )
    assert pdf.read_bytes() == payload
