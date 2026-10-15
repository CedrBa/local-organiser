"""Coordinate knowledge initialisation and application across DB and filesystem."""

from __future__ import annotations

from typing import Any, Callable
from sqlalchemy.orm import Session
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.domain import (
    ConflictError,
    EventContext,
)
from organiser.services.knowledge import (
    KnowledgeError,
    KnowledgeStore,
)
from .audit import KnowledgeAudit
from .binding import KnowledgeBinding
from .boundary import KnowledgeBoundary
from .contracts import (
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
    ContextKnowledgeResult,
    ContextKnowledgeWrite,
    KNOWLEDGE_INIT_COMPLETED_EVENT,
    KNOWLEDGE_INIT_FAILED_EVENT,
    KNOWLEDGE_INIT_INTENT_EVENT,
    KNOWLEDGE_INIT_NOOP_EVENT,
    KNOWLEDGE_WRITE_COMPLETED_EVENT,
    KNOWLEDGE_WRITE_FAILED_EVENT,
    KNOWLEDGE_WRITE_INTENT_EVENT,
    KNOWLEDGE_WRITE_NOOP_EVENT,
    OUTCOME_NOT_WRITTEN,
    OUTCOME_PARTIAL_STRUCTURE,
    OUTCOME_UNKNOWN,
    _ContextBinding,
    _NO_AUTOMATIC_RETRY,
    _PRE_WRITE_ERRORS,
    _VerificationFailed,
)
from .values import _digest, _operation_id, _relative_file, _timestamp
from .verification import KnowledgeVerification


class ContextKnowledgeService:
    """Coordinate explicit context knowledge operations and their audit trail.

    Owns each operation transaction and shares the store writer lock. Binding,
    audit and verification are components; no joint DB/filesystem atomicity or
    automatic recovery retry is promised.
    """

    def __init__(self, session: Session, store: KnowledgeStore) -> None:
        self._session = session
        self._store = store
        self._boundary = KnowledgeBoundary(session)
        # Callbacks preserve the explicit service boundary, including failure injection.
        self._audit = KnowledgeAudit(session, lambda: self._commit())
        self._binding = KnowledgeBinding(
            session, store, lambda *args, **kwargs: self._record(*args, **kwargs)
        )
        self._verification = KnowledgeVerification(store)

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    def _require_application_boundary(self):
        """Sicherstellen, dass hier eine eigene Application-Transaktion beginnt."""
        return self._boundary._require_application_boundary()

    def _own_transaction(self):
        """Eigene Transaktion: Eintritt pruefen, eigenen Fehler sauber abschliessen."""
        return self._boundary._own_transaction()

    def _assert_committed(self, record: _ContextBinding):
        """Sicherstellen, dass der Context weiterhin existiert und committet ist."""
        return self._boundary._assert_committed(record)

    def _commit(self):
        """Delegate to the operation component."""
        return self._boundary._commit()

    def _record(
        self,
        *,
        event_type: str,
        context_id: str,
        payload: dict[str, Any],
        event_context: EventContext | None,
        wrap_commit_error: bool = True,
    ):
        """Schreibe ein Audit-Ereignis und committe es sofort."""
        return self._audit._record(
            event_type=event_type,
            context_id=context_id,
            payload=payload,
            event_context=event_context,
            wrap_commit_error=wrap_commit_error,
        )

    def _record_if_reachable(
        self,
        *,
        event_type: str,
        context_id: str,
        payload: dict[str, Any],
        event_context: EventContext | None,
    ):
        """Fehlerereignis, aber nur soweit die Datenbank noch erreichbar ist."""
        return self._audit._record_if_reachable(
            event_type=event_type,
            context_id=context_id,
            payload=payload,
            event_context=event_context,
        )

    def _load(self, context_id: str):
        """Lade den verbindlichen DB-Stand eines Contexts."""
        return self._binding._load(context_id)

    def _resolve_path(self, knowledge_path: str, context_id: str):
        """Pruefe einen bereits gebundenen Pfad, ohne ihn zu veraendern."""
        return self._binding._resolve_path(knowledge_path, context_id)

    def _bind(self, record: _ContextBinding, operation_id: str, event_context: EventContext | None):
        """Pruefe oder binde den Pfad; Bindung und Ereignis werden gemeinsam committet."""
        return self._binding._bind(record, operation_id, event_context)

    def _inspect_core_structure(self, knowledge_path: str):
        """Zustand der regulaeren Grundstruktur: Verzeichnis da? Was fehlt?"""
        return self._binding._inspect_core_structure(knowledge_path)

    def _assert_core_structure(self, knowledge_path: str, context_id: str):
        """Delegate to the operation component."""
        return self._binding._assert_core_structure(knowledge_path, context_id)

    def ensure_context_knowledge(
        self, context_id: str, *, context: EventContext | None = None
    ) -> ContextKnowledgeResult:
        """Initialisiere den Wissensspeicher eines existierenden Contexts.

        Die Reihenfolge ist bewusst und auditrelevant:

        a) Eintritt pruefen: keine aktive Caller-Transaktion, kein Pending-State.
        b) Context laden. Ist er unbekannt, endet der Aufruf hier -- ohne jede
           Dateisystem-Nebenwirkung.
        c) Pfad pruefen oder binden (nur NULL -> Wert).
        d) Fehlende Struktur lesend feststellen; das Ergebnis entscheidet ueber
           ``changed`` und damit ueber Abschluss oder No-op.
        e) Bindung und Intent-Ereignis committen, also BEVOR etwas geschrieben wird.
        f) Fehlende Dateien und Ordner erzeugen; vorhandener Inhalt bleibt.
        g) Das persistente Ergebnis pruefen und Completion- bzw. No-op-Ereignis
           committen.

        Ein erneuter Aufruf ergaenzt nur Fehlendes und ist damit reparaturfaehig.
        """
        with self._own_transaction():
            operation_id = _operation_id()
            record = self._load(context_id)
            self._assert_committed(record)
            path, bound_now = self._bind(record, operation_id, context)
            directory_existed, missing_before = self._inspect_core_structure(path)

            self._record(
                event_type=KNOWLEDGE_INIT_INTENT_EVENT,
                context_id=record.context_id,
                payload={
                    "operation_id": operation_id,
                    "operation": "ensure_context_knowledge",
                    "knowledge_path": path,
                    "bound_now": bound_now,
                    "missing_before": list(missing_before),
                    "attempted_at": _timestamp(),
                },
                event_context=context,
            )

            try:
                structure = self._store.ensure_context_knowledge(path, title=record.title)
                _existed_after, still_missing = self._inspect_core_structure(path)
                if still_missing:
                    raise ContextKnowledgeError(
                        "Knowledge-Struktur unvollstaendig, trotz erfolgreichem Schreiben fehlt: "
                        + ", ".join(still_missing)
                    )
            except (KnowledgeError, OSError, ContextKnowledgeError) as error:
                self._record_if_reachable(
                    event_type=KNOWLEDGE_INIT_FAILED_EVENT,
                    context_id=record.context_id,
                    payload={
                        "operation_id": operation_id,
                        "operation": "ensure_context_knowledge",
                        "knowledge_path": path,
                        "outcome": OUTCOME_PARTIAL_STRUCTURE,
                        "missing_before": list(missing_before),
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "failed_at": _timestamp(),
                        "recovery": (
                            "Die Struktur kann teilinitialisiert sein. Der Context bleibt gebunden und "
                            "gueltig; ensure_context_knowledge ergaenzt nur Fehlendes und ist wiederholbar."
                        ),
                    },
                    event_context=context,
                )
                raise ContextKnowledgeError(
                    f"Knowledge-Initialisierung fehlgeschlagen ({error}). Der Speicher kann teilinitialisiert "
                    f"sein (vorher fehlte: {', '.join(missing_before) or 'nichts'}); die Transaktion wurde "
                    "zurueckgerollt, der Context bleibt gebunden und reparierbar."
                ) from error

            created_files = tuple(
                _relative_file(path, item, self._store.root) for item in structure.created_files
            )
            directory_created = bool(structure.created) or not directory_existed
            changed = bool(structure.created or structure.created_files or missing_before)
            self._record(
                event_type=KNOWLEDGE_INIT_COMPLETED_EVENT if changed else KNOWLEDGE_INIT_NOOP_EVENT,
                context_id=record.context_id,
                payload={
                    "operation_id": operation_id,
                    "operation": "ensure_context_knowledge",
                    "knowledge_path": path,
                    "changed": changed,
                    "directory_created": directory_created,
                    "created_files": list(created_files),
                    "repaired": list(missing_before),
                    "completed_at": _timestamp(),
                },
                event_context=context,
            )
            return ContextKnowledgeResult(
                context_id=record.context_id,
                knowledge_path=path,
                operation_id=operation_id,
                bound_now=bound_now,
                directory_created=directory_created,
                created_files=created_files,
                changed=changed,
            )

    def apply(
        self,
        update: ContextKnowledgeUpdate,
        *,
        context: EventContext | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> ContextKnowledgeWrite:
        """Wende ein context-gebundenes Update an.

        Ablauf: Context-ID -> verbindliche DB-Bindung -> vollstaendige
        Grundstruktur -> validiertes Store-Update. Der Pfad stammt ausschliesslich
        aus der Datenbank; die DTO kann keinen Pfad ausdruecken, und dieser Service
        baut das interne ``KnowledgeUpdate`` selbst.

        Eine Bindung ist keine Initialisierung. Fehlt ein Teil der regulaeren
        Grundstruktur, endet der Aufruf kontrolliert mit
        :class:`ContextKnowledgeNotInitializedError`, ohne Datei, Ordner oder Root
        anzulegen. Es wird *kein* Initialisierungs-Lebenszyklus nebenbei erzeugt:
        ein implizites ``ensure`` wuerde eine unvollstaendige Initialisierung als
        Nebenwirkung eines Schreibvorgangs erzeugen. Der Aufrufer ruft stattdessen
        bewusst :meth:`ensure_context_knowledge` auf.

        Es findet bewusst kein Zugriff auf ``ContextSource``-Dateien und keine
        automatische Discovery statt: Wissen wandert nur durch ein explizites
        Update.
        """
        with self._own_transaction(), self._store._lock:
            operation_id = _operation_id()
            record = self._load(update.context_id)
            self._assert_committed(record)
            if record.knowledge_path is None:
                raise ContextKnowledgeNotInitializedError(
                    f"Context {record.context_id} hat keinen gebundenen knowledge_path; "
                    "ensure_context_knowledge muss zuerst laufen"
                )
            path = self._resolve_path(record.knowledge_path, record.context_id)
            store_update = update.to_knowledge_update(path)
            expected_file = self._expected_file(store_update)
            self._assert_core_structure(path, record.context_id)

            self._record(
                event_type=KNOWLEDGE_WRITE_INTENT_EVENT,
                context_id=record.context_id,
                payload={
                    "operation_id": operation_id,
                    "operation": store_update.action,
                    "knowledge_path": path,
                    "file": expected_file,
                    "attempted_at": _timestamp(),
                },
                event_context=context,
            )

            # Proposal guards run after the durable intent, under the same lock
            # as the actual write. A refused guard has a provably unwritten outcome.
            if before_write is not None:
                try:
                    before_write()
                except ConflictError:
                    self._record_if_reachable(
                        event_type=KNOWLEDGE_WRITE_FAILED_EVENT,
                        context_id=record.context_id,
                        payload={
                            "operation_id": operation_id,
                            "operation": store_update.action,
                            "file": expected_file,
                            "outcome": OUTCOME_NOT_WRITTEN,
                            "error_type": "precondition_failed",
                        },
                        event_context=context,
                    )
                    raise

            try:
                write = self._store.apply(store_update)
            except (KnowledgeError, OSError) as error:
                not_written = isinstance(error, _PRE_WRITE_ERRORS)
                outcome = OUTCOME_NOT_WRITTEN if not_written else OUTCOME_UNKNOWN
                self._record_if_reachable(
                    event_type=KNOWLEDGE_WRITE_FAILED_EVENT,
                    context_id=record.context_id,
                    payload={
                        "operation_id": operation_id,
                        "operation": store_update.action,
                        "knowledge_path": path,
                        "file": expected_file,
                        "outcome": outcome,
                        "changed": None,
                        "error": str(error),
                        "error_type": type(error).__name__,
                        "failed_at": _timestamp(),
                        "recovery": (
                            "Die Eingabe wurde abgelehnt, ohne eine Datei zu veraendern; nach Korrektur "
                            "bewusst erneut ausfuehren."
                            if not_written
                            else _NO_AUTOMATIC_RETRY
                        ),
                    },
                    event_context=context,
                )
                if not_written:
                    raise ContextKnowledgeError(
                        f"Knowledge-Schreibvorgang '{store_update.action}' auf {expected_file} wurde "
                        f"abgelehnt, ohne eine Datei zu veraendern: {error}"
                    ) from error
                raise ContextKnowledgeAmbiguousError(
                    f"Knowledge-Schreibvorgang '{store_update.action}' auf {expected_file} ist "
                    f"abgebrochen, der Dateizustand dabei ungeklaert ({error}). Audit: Intent + "
                    f"FAILED mit outcome={OUTCOME_UNKNOWN}. {_NO_AUTOMATIC_RETRY}"
                ) from error

            try:
                verified = self._verified_content(path, expected_file, write.content)
            except _VerificationFailed as defect:
                self._record_if_reachable(
                    event_type=KNOWLEDGE_WRITE_FAILED_EVENT,
                    context_id=record.context_id,
                    payload={
                        "operation_id": operation_id,
                        "operation": store_update.action,
                        "knowledge_path": path,
                        "file": expected_file,
                        "outcome": OUTCOME_UNKNOWN,
                        "changed": None,
                        "error": str(defect),
                        "error_type": "VerificationFailed",
                        "failed_at": _timestamp(),
                        "recovery": _NO_AUTOMATIC_RETRY,
                    },
                    event_context=context,
                )
                raise ContextKnowledgeAmbiguousError(
                    f"Knowledge-Schreibvorgang '{store_update.action}' auf {expected_file} ist "
                    f"unbestaetigt: {defect}. Es wird kein Abschluss behauptet. {_NO_AUTOMATIC_RETRY}"
                ) from defect

            relative = _relative_file(path, write.path, self._store.root)
            content_hash = _digest(verified)
            try:
                self._record(
                    event_type=KNOWLEDGE_WRITE_COMPLETED_EVENT
                    if write.changed
                    else KNOWLEDGE_WRITE_NOOP_EVENT,
                    context_id=record.context_id,
                    payload={
                        "operation_id": operation_id,
                        "operation": store_update.action,
                        "knowledge_path": path,
                        "file": relative,
                        "changed": write.changed,
                        "content_hash": content_hash,
                        "completed_at": _timestamp(),
                    },
                    event_context=context,
                    wrap_commit_error=False,
                )
            except Exception as error:
                # Die Datei ist geschrieben, das Abschlussereignis nicht. Erfolg wird
                # nicht behauptet, die Datei nicht zurueckgeloescht und der nicht
                # idempotente Vorgang nicht automatisch wiederholt.
                raise ContextKnowledgeAmbiguousError(
                    f"Knowledge-Schreibvorgang '{store_update.action}' auf {relative} war erfolgreich, "
                    f"der abschliessende Audit-Commit ist jedoch fehlgeschlagen ({error}). "
                    "Audit-Zustand ungeklaert: Intent ohne Abschluss. Die Datei wurde nicht "
                    "zurueckgeloescht; bitte pruefen und nur bewusst erneut ausfuehren."
                ) from error

            return ContextKnowledgeWrite(
                context_id=record.context_id,
                knowledge_path=path,
                operation_id=operation_id,
                file=relative,
                changed=write.changed,
                content_hash=content_hash,
            )

    def _verified_content(self, knowledge_path: str, relative_file: str, expected: str):
        """Lese die Zieldatei zurueck und belege das erwartete Ergebnis."""
        return self._verification._verified_content(knowledge_path, relative_file, expected)

    _expected_file = staticmethod(KnowledgeVerification._expected_file)
