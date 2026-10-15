"""Shared fixtures and builders for test_workspace_ingestion_extraction contracts; no executable tests."""

from __future__ import annotations

import glob
from pathlib import Path
import pytest
from organiser.services.extraction import (
    pypdf_version,
)
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
)

PDF_AVAILABLE = pypdf_version() is not None

requires_pypdf = pytest.mark.skipif(not PDF_AVAILABLE, reason="pypdf ist nicht installiert")


@pytest.fixture
def course(tmp_path):
    """A context with one allowed folder source and one markdown document."""
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung der Notiz", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        close_boundary(session)
        source_id = seed_source_id(session, context_id, path=str(root))
    return settings, context_id, source_id, root


def scan_and_get_resource_id(settings, context_id, source_id, relative_path="Notiz.md") -> str:
    from organiser.services.resources import scan_source

    with session_for(settings) as session:
        scan_source(session, source_id, settings)
        resource_id = resource_by_locator(
            session, context_id=context_id, relative_path=relative_path
        ).id
    return resource_id


# -- parser isolation: hostile workers ---------------------------------------


@pytest.fixture
def hostile_worker(tmp_path):
    def write(name: str, body: str) -> Path:
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        return path

    return write


def _workdirs() -> set[str]:
    return set(glob.glob("/tmp/organiser-pdf-*"))
