"""Non-leaking manifest change diagnostics and verification results."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from .redaction import _contains_absolute_path, _looks_like_absolute_path, _truncate
from .verification_contracts import (
    _MAX_CHANGE_TEXT,
    _MAX_REFERENCE_CHARS,
    _REFERENCE_PATH_MARKER,
    _REFERENCE_UNSAFE_MARKER,
)


@dataclass(frozen=True)
class WorkspaceManifestChange:
    """One difference between a recorded manifest and the current state.

    Two different rules, because ``reference`` and the rest are used for different
    things.

    ``reference`` is the **machine** value: an entity id, a snapshot id or a
    store-relative knowledge address. A caller maps a change back to the row it belongs
    to, and a legal knowledge address is up to 512 characters plus the store file name,
    so truncating or rewriting it would destroy exactly the mapping the caller needs.
    It is therefore **validated, not sanitised**: a reference that is safe -- bounded,
    printable, without markup delimiters and not a host address -- is kept exactly, and
    only an unsafe one is replaced by a fixed marker. Sanitising instead of refusing
    would hand back a value that looks like a real reference but names nothing.

    ``field``, ``expected`` and ``actual`` are **display** values derived from an
    untrusted record. They are stripped of control characters and angle brackets and
    capped, because a verifier is not an echo service and whatever it reports may end up
    in an API error body. :meth:`describe` applies the same treatment to ``reference``
    for display, and nothing else: the machine value stays exact, the human line stays
    bounded.
    """

    kind: str
    reference: str
    field: str
    expected: str
    actual: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "reference", _reference_text(self.reference))
        for name in ("field", "expected", "actual"):
            object.__setattr__(self, name, _change_text(getattr(self, name)))

    def describe(self) -> str:
        return (
            f"{self.kind} {_change_text(self.reference)}: {self.field} erwartet "
            f"{_change_text(self.expected)!r}, aktuell {_change_text(self.actual)!r}"
        )


def _reference_text(value: Any) -> str:
    """The machine reference: exact when it is safe, a fixed marker when it is not.

    Safe means the value this module itself could have produced: bounded by
    :data:`_MAX_REFERENCE_CHARS`, printable, free of markup delimiters and free of host
    addresses -- whether the value *is* one or merely *contains* one, since a reference
    like ``"plan=/home/operator/GEHEIM/x.pdf"`` names a host just as well as a bare path
    does. A legitimate store-relative address is unaffected: no segment of a relative
    path can begin with ``/`` or ``\\``, so ``kurse/teil 1/overview.md`` stays exact.

    Refusing rather than rewriting is the point -- a rewritten address would be
    indistinguishable from a real one and would send a caller looking for a row that
    does not exist.
    """
    text = value if isinstance(value, str) else _change_text(value)
    if not text:
        return text
    if _looks_like_absolute_path(text) or _contains_absolute_path(text):
        return _REFERENCE_PATH_MARKER
    if len(text) > _MAX_REFERENCE_CHARS:
        return _REFERENCE_UNSAFE_MARKER
    if any(not character.isprintable() or character in "<>" for character in text):
        return _REFERENCE_UNSAFE_MARKER
    return text


def _change_text(value: Any, limit: int = _MAX_CHANGE_TEXT) -> str:
    """Render an untrusted manifest value safely for a diagnostic line."""
    if value is None:
        return ""
    if not isinstance(value, str):
        try:
            value = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):  # pragma: no cover - default=str covers almost all
            value = str(value)
    # Angle brackets are what makes an echoed string dangerous in HTML; control
    # characters are what makes it dangerous in a terminal. A plain space is printable
    # and is deliberately kept -- these are diagnostics, not identifiers.
    safe = "".join(
        character for character in value if character.isprintable() and character not in "<>"
    )
    return _truncate(safe, max(16, limit))[0] or ""


@dataclass(frozen=True)
class WorkspaceManifestVerification:
    """Result of a read-only manifest re-check. ``unchanged`` is the apply gate."""

    unchanged: bool
    changes: tuple[WorkspaceManifestChange, ...] = ()
    checked_entities: int = 0
    checked_snapshots: int = 0
    checked_knowledge_files: int = 0

    def describe(self) -> str:
        if self.unchanged:
            return "Manifest unveraendert"
        return "; ".join(change.describe() for change in self.changes)
