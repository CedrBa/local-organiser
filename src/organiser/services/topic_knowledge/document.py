"""Synthetic topic placeholder content; no file or database IO."""

from __future__ import annotations

from organiser.services.knowledge import (
    _Document,
)
from .contracts import NOTES_SECTION


def placeholder_document(title: str) -> str:
    """Render an explicit topic placeholder without writing files or changing DB state."""
    return _Document(
        title,
        f"# {title}",
        [
            (
                NOTES_SECTION,
                f'Automatisch angelegter Platzhalter fuer das Topic "{title}". '
                "Der fachliche Titel und der Topic-Status werden in der Datenbank gefuehrt; "
                "diese Datei enthaelt die qualitativen Notizen und den Lernfortschritt.",
            )
        ],
    ).render()
