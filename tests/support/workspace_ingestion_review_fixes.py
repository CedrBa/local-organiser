"""Shared fixtures and builders for test_workspace_ingestion_review_fixes contracts; no executable tests."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from sqlalchemy import select
from organiser.db.models import ContextSource, Resource
from organiser.services.resources import (
    scan_source,
)
from support.ingestion import (
    close_boundary,
    make_settings,
    resource_by_locator,
    seed_context,
    seed_source_id,
    session_for,
)

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


class _CountingScanner:
    """``os.scandir`` wrapper that counts how many entries were actually pulled.

    It exists to prove that a directory is *not* materialised before the budget is
    charged: if the walk still read a whole directory, this counter would be far
    larger than ``max_entries``.
    """

    def __init__(self, scanner, counter: list[int]) -> None:
        self._scanner = scanner
        self._counter = counter

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self._scanner)
        self._counter[0] += 1
        return entry

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return self._scanner.__exit__(*exc_info)


def _scanned_course(tmp_path):
    """A context with one folder source and one markdown document, scanned once."""
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung der Notiz", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,))
    with session_for(settings) as session:
        context_id = seed_context(session).id
        source_id = seed_source_id(session, context_id, path=str(root))
        close_boundary(session)
        scan_source(session, source_id, settings)
    return settings, context_id, source_id, root


def _resource_id_of(session, context_id: str) -> str:
    """The id of the registered ``Notiz.md`` of a course fixture."""
    resource = resource_by_locator(session, context_id=context_id, relative_path="Notiz.md")
    assert resource is not None
    return resource.id


def _refill_after_stat(target, content: str = "zweite Fassung"):
    """A writer that refills the file right *after* the scan looked at it.

    The scan is made to observe the truncated file and to see the new bytes arrive
    immediately afterwards, which is the strongest version of the race: the empty
    observation and the finished file are almost simultaneous.
    """
    os.truncate(target, 0)
    real_fstat = os.fstat
    seen: dict[int, int] = {}

    def late_write_fstat(fd):
        status = real_fstat(fd)
        seen[fd] = seen.get(fd, 0) + 1
        if stat.S_ISREG(status.st_mode) and status.st_size == 0 and seen[fd] >= 1:
            target.write_text(content, encoding="utf-8")
        return status

    return late_write_fstat


# -- F3: a resource that lost its source gets a fresh, checked owner -----------


def _orphan_the_source(session, source_id: str) -> None:
    session.execute(ContextSource.__table__.delete().where(ContextSource.id == source_id))
    session.commit()


def resource_id_of(session, context_id: str, locator: Path) -> str:
    return session.scalar(
        select(Resource.id).where(
            Resource.context_id == context_id, Resource.locator == str(locator)
        )
    )
