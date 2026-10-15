"""Knowledge operation identity, timestamps and exact relative file references."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _digest(content: str) -> str:
    """Kurzer, stabiler Inhalts-Hash fuer das Audit-Log."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def _timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _operation_id() -> str:
    """Eindeutige Kennung eines Vorgangs; verbindet Intent und Abschluss."""
    return uuid.uuid4().hex


def _relative_file(knowledge_path: str, path, root: Path) -> str:
    """Pfad relativ zum Kontextverzeichnis, damit das Log keine Root-Pfade leakt."""
    try:
        return str(Path(path).relative_to(root / knowledge_path))
    except ValueError:
        return Path(path).name
