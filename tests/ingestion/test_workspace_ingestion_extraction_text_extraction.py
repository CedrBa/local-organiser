"""Text extraction contracts for ingestion."""

from __future__ import annotations

from organiser.domain.resources import (
    DEFAULT_LIMITS,
    SnapshotStatus,
)
from organiser.services.extraction import (
    TEXT_EXTRACTOR_VERSION,
    extract_text_bytes,
)
from support.workspace_ingestion_extraction import (
    PDF_AVAILABLE as PDF_AVAILABLE,
    requires_pypdf as requires_pypdf,
    course as course,
    scan_and_get_resource_id as scan_and_get_resource_id,
    hostile_worker as hostile_worker,
    _workdirs as _workdirs,
)


# -- text extraction ---------------------------------------------------------


def test_utf8_text_is_extracted_and_truncated_at_the_character_budget():
    outcome = extract_text_bytes("Hallo Welt".encode("utf-8"))
    assert outcome.status is SnapshotStatus.READY
    assert outcome.text == "Hallo Welt"
    assert outcome.extractor_version == TEXT_EXTRACTOR_VERSION
    assert outcome.character_count == 10
    assert outcome.truncated is False

    long_text = "ä" * (DEFAULT_LIMITS.max_text_characters + 500)
    outcome = extract_text_bytes(
        long_text.encode("utf-8"), limits=DEFAULT_LIMITS.narrowed(max_text_characters=100)
    )
    assert outcome.truncated is True
    assert outcome.character_count == 100
    assert outcome.text == "ä" * 100
