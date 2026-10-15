"""Verify persisted content and expected destinations after file operations."""

from __future__ import annotations

from organiser.domain.knowledge import KnowledgeUpdate
from organiser.services.knowledge import (
    KnowledgeError,
    KnowledgeStore,
    _slug,
)
from .contracts import ContextKnowledgeError, TOPICS_DIRECTORY, _VerificationFailed
from .values import _digest


class KnowledgeVerification:
    """Prove persisted file contents and expected destinations after writes."""

    def __init__(self, store: KnowledgeStore) -> None:
        self._store = store

    def _verified_content(self, knowledge_path: str, relative_file: str, expected: str) -> str:
        """Lese die Zieldatei zurueck und belege das erwartete Ergebnis.

        Der zurueckgegebene Inhalt des Stores ist eine *Absicht*, kein Beleg: ein
        Fehler nach ``os.replace`` oder eine still nicht ausgefuehrte Ersetzung
        liefert trotzdem Text. Erst der erneute Lesevorgang auf der Platte belegt
        den Zustand; nur er darf einen bestaetigenden Abschluss tragen und nur
        sein Inhalt gehoert in den Audit-Hash.
        """
        try:
            persisted = self._store.read(f"{knowledge_path}/{relative_file}")
        except (KnowledgeError, OSError) as error:
            raise _VerificationFailed(
                f"Zieldatei '{relative_file}' nicht lesbar: {error}"
            ) from error
        if persisted != expected:
            raise _VerificationFailed(
                f"persistenter Inhalt von '{relative_file}' weicht vom erwarteten Ergebnis ab "
                f"(erwartet {_digest(expected)}, auf der Platte {_digest(persisted)})"
            )
        return persisted

    @staticmethod
    def _expected_file(update: KnowledgeUpdate) -> str:
        """Erwartete Zieldatei einer Aktion, ohne den Store zu befragen.

        Bei Topics ist der *wirkliche* Dateiname entscheidend: er entsteht aus dem
        Slug des Titels, nicht aus dem Titel selbst. Der Intent nennt deshalb
        denselben Namen wie der spaetere Abschluss -- bei einem fehlenden Abschluss
        ist er das einzige Recovery-Indiz. Die reine Funktion ``_slug`` wird ohne
        Dateisystemzugriff benutzt (``topic_path`` waere ein FS-Nebeneffekt).
        """
        if update.action == "update_overview":
            return "overview.md"
        if update.action == "update_progress":
            return "progress.md"
        if update.action == "append_note":
            return "notes.md"
        try:
            stem = _slug(update.topic or "")
        except KnowledgeError as error:
            raise ContextKnowledgeError(
                f"Topic-Titel ergibt keinen gueltigen Dateinamen: {error}"
            ) from error
        return f"{TOPICS_DIRECTORY}/{stem}.md"
