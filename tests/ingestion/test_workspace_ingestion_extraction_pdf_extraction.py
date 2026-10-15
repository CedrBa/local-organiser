"""Pdf extraction contracts for ingestion."""

from __future__ import annotations

from organiser.domain.resources import (
    DEFAULT_LIMITS,
    SnapshotStatus,
)
from organiser.services import extraction
from organiser.services.extraction import (
    CODE_PDF_OUTPUT_LIMIT,
    CODE_PDF_TIMEOUT,
    CODE_PDF_UNAVAILABLE,
    TEXT_EXTRACTOR_VERSION,
    extract_bytes,
    extract_text_bytes,
    extractor_version_for,
    pdf_extractor_version,
    run_pdf_worker,
)
from support.ingestion import (
    make_image_only_pdf_bytes,
    make_malformed_pdf_bytes,
    make_pdf_bytes,
)
from support.workspace_ingestion_extraction import (
    PDF_AVAILABLE as PDF_AVAILABLE,
    requires_pypdf as requires_pypdf,
    course as course,
    scan_and_get_resource_id as scan_and_get_resource_id,
    hostile_worker as hostile_worker,
    _workdirs as _workdirs,
)


def test_non_utf8_text_is_unsupported_not_guessed():
    outcome = extract_text_bytes(b"Gr\xfc\xdfe aus Latin-1")
    assert outcome.status is SnapshotStatus.UNSUPPORTED
    assert outcome.error_code == "text_not_utf8"
    assert outcome.text is None


def test_empty_file_is_failed_not_ready():
    outcome = extract_text_bytes(b"")
    assert outcome.status is SnapshotStatus.FAILED
    assert outcome.error_code == "empty_file"


def test_unsupported_resource_type_has_no_extractor():
    assert extractor_version_for("other") is None
    assert extractor_version_for("markdown") == TEXT_EXTRACTOR_VERSION
    outcome = extract_bytes("other", b"\x00\x01")
    assert outcome.status is SnapshotStatus.UNSUPPORTED
    assert outcome.error_code == "unsupported_resource_type"


# -- PDF through the isolated parser -----------------------------------------


@requires_pypdf
def test_pdf_text_is_extracted_with_page_and_character_provenance():
    payload = make_pdf_bytes(["Hallo Welt", "Zweite Seite"])
    outcome = extract_bytes("pdf", payload)
    assert outcome.status is SnapshotStatus.READY
    assert "Hallo Welt" in outcome.text
    assert outcome.page_count == 2
    assert outcome.metadata["pages"] == [
        {"page": 1, "char_start": 0, "char_end": 10},
        {"page": 2, "char_start": 12, "char_end": 24},
    ]
    assert outcome.extractor_version == pdf_extractor_version()
    assert outcome.metadata["parser"].startswith("pypdf-")


@requires_pypdf
def test_image_only_pdf_is_unsupported_and_needs_ocr():
    outcome = extract_bytes("pdf", make_image_only_pdf_bytes())
    assert outcome.status is SnapshotStatus.UNSUPPORTED
    assert outcome.error_code == "pdf_needs_ocr"
    assert outcome.text is None


@requires_pypdf
def test_malformed_pdf_is_failed_with_a_code():
    outcome = extract_bytes("pdf", make_malformed_pdf_bytes())
    assert outcome.status is SnapshotStatus.FAILED
    assert outcome.error_code == "pdf_parse_error"


@requires_pypdf
def test_pdf_page_budget_truncates_and_marks_it():
    outcome = extract_bytes(
        "pdf",
        make_pdf_bytes([f"Seite {index}" for index in range(5)]),
        limits=DEFAULT_LIMITS.narrowed(max_pdf_pages=2),
    )
    assert outcome.status is SnapshotStatus.READY
    assert outcome.truncated is True
    assert "Seite 0" in outcome.text
    assert "Seite 4" not in outcome.text
    assert [span["page"] for span in outcome.metadata["pages"]] == [1, 2]


@requires_pypdf
def test_pdf_text_budget_truncates_within_a_page():
    outcome = extract_bytes(
        "pdf",
        make_pdf_bytes(["x" * 500]),
        limits=DEFAULT_LIMITS.narrowed(max_text_characters=100),
    )
    assert outcome.character_count == 100
    assert outcome.truncated is True


def test_missing_parser_dependency_is_unsupported(tmp_path, monkeypatch):
    monkeypatch.setattr(extraction, "pypdf_version", lambda: None)
    outcome = extract_bytes("pdf", make_pdf_bytes(["Hallo"]))
    assert outcome.status is SnapshotStatus.UNSUPPORTED
    assert outcome.error_code == "pdf_dependency_missing"


def test_missing_worker_is_reported_not_raised(tmp_path):
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=tmp_path / "gibt-es-nicht.py")
    assert outcome.ok is False
    assert outcome.error_code == CODE_PDF_UNAVAILABLE


def test_hanging_parser_is_killed_after_the_timeout(hostile_worker):
    worker = hostile_worker(
        "hang.py", "import sys, time\nsys.stdin.buffer.read()\ntime.sleep(600)\n"
    )
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=0.5)
    assert outcome.ok is False
    assert outcome.error_code == CODE_PDF_TIMEOUT


def test_output_explosion_is_stopped_immediately(hostile_worker):
    worker = hostile_worker(
        "boom.py",
        "import sys\nsys.stdin.buffer.read()\n"
        "chunk = b'x' * 200000\n"
        "[sys.stdout.buffer.write(chunk) for _ in range(500)]\n",
    )
    outcome = run_pdf_worker(b"%PDF-1.4", worker_path=worker, timeout=60)
    assert outcome.ok is False
    assert outcome.error_code == CODE_PDF_OUTPUT_LIMIT
