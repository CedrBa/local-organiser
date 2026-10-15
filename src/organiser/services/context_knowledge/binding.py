"""Fresh immutable context bindings and explicit store initialisation checks."""

from __future__ import annotations

from typing import Callable
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from organiser.db.models import Context
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    default_knowledge_path,
    validate_context_path,
)
from organiser.services.domain import (
    ConflictError,
    EventContext,
    NotFoundError,
    _assert_knowledge_path_available,
)
from organiser.services.knowledge import (
    KnowledgeStore,
)
from .contracts import (
    CORE_FILES,
    CORE_STRUCTURE,
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
    KNOWLEDGE_PATH_BOUND_EVENT,
    TOPICS_DIRECTORY,
    _ContextBinding,
)
from .values import _timestamp


class KnowledgeBinding:
    """Resolve fresh immutable bindings and inspect the existing store structure."""

    def __init__(
        self, session: Session, store: KnowledgeStore, record: Callable[..., None]
    ) -> None:
        self._session = session
        self._store = store
        self._record = record

    def _load(self, context_id: str) -> _ContextBinding:
        """Lade den verbindlichen DB-Stand eines Contexts.

        Bewusst eine Spaltenabfrage statt ``session.get``: eine Entity-Abfrage
        liefert bei einem bereits geladenen Objekt den gecachten, moeglicherweise
        veralteten Zustand. Genau daraus wuerde ein ueberschriebener ``NULL``
        entstehen. Die Zeile ist ausserdem nicht in der Identitaetskarte, kann also
        von diesem Service nicht versehentlich mitgeaendert werden. Nach einer
        Bindung synchronisiert das bedingte UPDATE die Identitaetskarte des
        Aufrufers, damit dort kein stilles ``NULL`` zurueckbleibt.
        """
        if not isinstance(context_id, str) or not context_id.strip():
            raise ContextKnowledgeError("context_id darf nicht leer sein")
        row = self._session.execute(
            select(Context.id, Context.title, Context.knowledge_path).where(
                Context.id == context_id
            )
        ).one_or_none()
        if row is None:
            raise NotFoundError("Context nicht gefunden")
        return _ContextBinding(
            context_id=row.id, title=row.title, knowledge_path=row.knowledge_path
        )

    def _resolve_path(self, knowledge_path: str, context_id: str) -> str:
        """Pruefe einen bereits gebundenen Pfad, ohne ihn zu veraendern.

        Ungueltige, reservierte oder doppelte Altwerte werden nicht still
        umbenannt: der Aufrufer bekommt eine konkrete Diagnose und entscheidet.
        """
        try:
            path = validate_context_path(knowledge_path)
        except KnowledgePathError as error:
            raise ConflictError(
                f"Gebundener knowledge_path von Context {context_id} ist ungueltig und wird nicht "
                f"automatisch korrigiert: {error}. Manuelle Bereinigung noetig."
            ) from error
        owner = self._session.scalar(
            select(Context.id)
            .where(Context.knowledge_path == path, Context.id != context_id)
            .limit(1)
        )
        if owner is not None:
            raise ConflictError(
                f"Gebundener knowledge_path von Context {context_id} ist doppelt vergeben "
                f"(zusaetzlich Context {owner}). Manuelle Bereinigung noetig."
            )
        return path

    def _bind(
        self, record: _ContextBinding, operation_id: str, event_context: EventContext | None
    ) -> tuple[str, bool]:
        """Pruefe oder binde den Pfad; Bindung und Ereignis werden gemeinsam committet.

        Der Default haengt nur an der Context-ID und ist damit bei Umbenennung und
        Reparenting stabil. Ein bei der manuellen Anlage explizit gesetzter Pfad
        bleibt unveraendert bestehen.

        Gebunden wird ausschliesslich mit ``WHERE knowledge_path IS NULL``. Damit
        kann weder eine zwischenzeitliche Fremdbindung noch ein veralteter
        ORM-Zustand ueberschrieben werden.
        """
        if record.knowledge_path is not None:
            return self._resolve_path(record.knowledge_path, record.context_id), False

        candidate = validate_context_path(default_knowledge_path(record.context_id))
        _assert_knowledge_path_available(self._session, candidate)
        try:
            outcome = self._session.execute(
                update(Context)
                .where(Context.id == record.context_id, Context.knowledge_path.is_(None))
                .values(knowledge_path=candidate)
                # ``fetch`` haelt die Identitaetskarte des Aufrufers in Ordnung: ein
                # bereits geladener Context zeigt danach den gebundenen Wert statt
                # eines stillen ``NULL``. Es werden keine fremden Werte geschrieben --
                # unter dem Eintrittsschutz kann der Aufrufer nichts Uncommittetes
                # gehalten haben.
                .execution_options(synchronize_session="fetch")
            )
        except IntegrityError as error:
            # Der partielle Unique-Index ist die letzte Zusage der Datenbank.
            self._session.rollback()
            raise ConflictError(
                f"Der Default-Pfad {candidate} fuer Context {record.context_id} ist inzwischen "
                "vergeben; es wurde keine Bindung vorgenommen."
            ) from error

        if outcome.rowcount != 1:
            # Zwischen Pruefung und Bindung hat eine andere Transaktion gebunden.
            # Gebunden heisst dauerhaft: der fremde Wert gilt, es wird nicht
            # ueberschrieben und nichts zurueckgeschrieben.
            fresh = self._load(record.context_id)
            if fresh.knowledge_path is None:
                raise ContextKnowledgeError(
                    f"Context {record.context_id} liess sich nicht binden; der DB-Stand ist weiterhin "
                    "ungebunden und es wurden keine Dateien angelegt."
                )
            return self._resolve_path(fresh.knowledge_path, fresh.context_id), False

        self._record(
            event_type=KNOWLEDGE_PATH_BOUND_EVENT,
            context_id=record.context_id,
            payload={
                "operation_id": operation_id,
                "knowledge_path": candidate,
                "reason": "default",
                "bound_at": _timestamp(),
            },
            event_context=event_context,
        )
        return candidate, True

    def _inspect_core_structure(self, knowledge_path: str) -> tuple[bool, list[str]]:
        """Zustand der regulaeren Grundstruktur: Verzeichnis da? Was fehlt?

        Liest nur. Es wird weder der Knowledge Root noch ein Ordner oder eine
        Datei angelegt: eine gebundene, aber nicht initialisierte Adresse wird
        abgewiesen, statt nebenbei eine Teilinitialisierung zu erzeugen.
        """
        root = self._store.root
        if not root.is_dir():
            return False, list(CORE_STRUCTURE)
        # Der pfadsichere Zugriff des Stores; er legt nichts an, weil der Root
        # existiert, und weist Symlinks zurueck.
        directory = self._store.context_directory(knowledge_path)
        if directory.is_symlink() or not directory.is_dir():
            return False, list(CORE_STRUCTURE)
        missing = [
            name
            for name in CORE_FILES
            if (directory / name).is_symlink() or not (directory / name).is_file()
        ]
        topics = directory / TOPICS_DIRECTORY
        if topics.is_symlink() or not topics.is_dir():
            missing.append(TOPICS_DIRECTORY)
        return True, missing

    def _assert_core_structure(self, knowledge_path: str, context_id: str) -> None:
        _existed, missing = self._inspect_core_structure(knowledge_path)
        if missing:
            raise ContextKnowledgeNotInitializedError(
                f"Knowledge-Speicher '{knowledge_path}' von Context {context_id} ist nicht vollstaendig "
                f"initialisiert (fehlt: {', '.join(missing)}). ensure_context_knowledge muss zuerst "
                "laufen; es wurde nichts geschrieben und nichts angelegt."
            )
