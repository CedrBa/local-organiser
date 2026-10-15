"""Deterministic topic slug validation; no filesystem side effects."""

from __future__ import annotations

import re
import unicodedata
from .contracts import KnowledgeContentError, MAX_TOPIC_NAME_LENGTH, _UNSAFE_SEGMENT


def _slug(value: str) -> str:
    """Turn a human topic title into a stable, readable file stem.

    A normal semantic title is slugged deterministically. A title that is obviously
    path-shaped (separators, traversal, control characters) is rejected outright
    instead of being silently rewritten into a different, harmless-looking name:
    silently accepting ``../../etc/passwd`` would hide a caller bug.
    """
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeContentError("Topic-Titel darf nicht leer sein")
    if "/" in value or "\\" in value:
        raise KnowledgeContentError(f"Topic-Titel darf kein Pfad sein: {value!r}")
    if _UNSAFE_SEGMENT.search(value):
        raise KnowledgeContentError(f"Topic-Titel enthaelt Steuerzeichen: {value!r}")
    if ".." in value:
        raise KnowledgeContentError(f"Topic-Titel enthaelt Traversal: {value!r}")
    if value != value.strip():
        raise KnowledgeContentError(f"Topic-Titel hat Randleerzeichen: {value!r}")
    normalized = unicodedata.normalize("NFKD", value.strip())
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", ascii_only)
    cleaned = re.sub(r"[-_ ]+", "-", cleaned).strip("-. ")
    cleaned = cleaned[:MAX_TOPIC_NAME_LENGTH].strip("-. ")
    if not cleaned or set(cleaned) <= {"."}:
        raise KnowledgeContentError(f"Topic-Titel ergibt keinen verwendbaren Dateinamen: {value!r}")
    return cleaned
