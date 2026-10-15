"""Shared fixtures and builders for test_knowledge_regressions contracts; no executable tests."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
import pytest
from organiser.services.knowledge import (
    KnowledgeStore,
)

CONTEXT = "UNI/Mathe/Mathe 2"

T1 = datetime(2026, 9, 29, 8, 0, tzinfo=UTC)

T2 = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)


def write_core_file(knowledge_store: KnowledgeStore, name: str, content: str) -> Path:
    knowledge_store.ensure_context_knowledge(CONTEXT, title="Mathe 2")
    path = knowledge_store.context_directory(CONTEXT) / name
    path.write_text(content, encoding="utf-8")
    return path


# --- 2. Zugriffsschutz vor dem Zugriff --------------------------------------


class AccessRecorder:
    """Records every ``read_text`` and directory listing the store performs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads: list[str] = []
        self.scans: list[str] = []
        original_read = Path.read_text
        original_scandir = os.scandir

        def read_text(self: Path, *args, **kwargs):
            self.reads.append(str(self))
            return original_read(self, *args, **kwargs)

        def scandir(path="."):
            self.scans.append(str(path))
            return original_scandir(path)

        monkeypatch.setattr(Path, "read_text", read_text)
        monkeypatch.setattr(os, "scandir", scandir)

    def forget(self) -> None:
        self.reads.clear()
        self.scans.clear()


#: Ungepatchtes ``Path.read_text`` fuer eigene Assertions in den Tests.
ORIGINAL_READ_TEXT = Path.read_text


def knowledge_root_entries(knowledge_store: KnowledgeStore):
    return list(knowledge_store.root.iterdir()) if knowledge_store.root.exists() else []
