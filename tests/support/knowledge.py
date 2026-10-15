"""Shared fixtures and builders for test_knowledge contracts; no executable tests."""

from __future__ import annotations

from pathlib import Path

MATHE = "UNI/Mathe/Mathe 2"

STATISTIK = "UNI/Statistik"


def _write_core_file(knowledge_store, name: str, content: str) -> Path:
    """Overwrite a core file with hand-written markdown."""
    knowledge_store.ensure_context_knowledge(MATHE, title="Mathe 2")
    path = knowledge_store.context_directory(MATHE) / name
    path.write_text(content, encoding="utf-8")
    return path
