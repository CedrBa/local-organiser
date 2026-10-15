"""Context-bound knowledge results, audit vocabulary and ambiguous-outcome errors."""

from __future__ import annotations

from dataclasses import dataclass
from organiser.services.domain import (
    DomainError,
)
from organiser.services.knowledge import (
    KnowledgeContentError,
    UnsafeKnowledgePathError,
)


CORE_FILES = ("overview.md", "progress.md", "notes.md")


TOPICS_DIRECTORY = "topics"


CORE_STRUCTURE = (*CORE_FILES, TOPICS_DIRECTORY)


# Ereignistypen. Intent steht immer VOR der Dateisystemmutation, Abschluss NACH
# der Pruefung des persistenten Ergebnisses. Ein Intent ohne Abschluss bedeutet
# "ungeklaert" (siehe ADR).
KNOWLEDGE_PATH_BOUND_EVENT = "CONTEXT_KNOWLEDGE_PATH_BOUND"


KNOWLEDGE_INIT_INTENT_EVENT = "CONTEXT_KNOWLEDGE_INIT_INTENT"


KNOWLEDGE_INIT_COMPLETED_EVENT = "CONTEXT_KNOWLEDGE_INIT_COMPLETED"


KNOWLEDGE_INIT_NOOP_EVENT = "CONTEXT_KNOWLEDGE_INIT_NOOP"


KNOWLEDGE_INIT_FAILED_EVENT = "CONTEXT_KNOWLEDGE_INIT_FAILED"


KNOWLEDGE_WRITE_INTENT_EVENT = "CONTEXT_KNOWLEDGE_WRITE_INTENT"


KNOWLEDGE_WRITE_COMPLETED_EVENT = "CONTEXT_KNOWLEDGE_WRITE_COMPLETED"


KNOWLEDGE_WRITE_NOOP_EVENT = "CONTEXT_KNOWLEDGE_WRITE_NOOP"


KNOWLEDGE_WRITE_FAILED_EVENT = "CONTEXT_KNOWLEDGE_WRITE_FAILED"


_KNOWLEDGE_EVENTS = frozenset(
    {
        KNOWLEDGE_PATH_BOUND_EVENT,
        KNOWLEDGE_INIT_INTENT_EVENT,
        KNOWLEDGE_INIT_COMPLETED_EVENT,
        KNOWLEDGE_INIT_NOOP_EVENT,
        KNOWLEDGE_INIT_FAILED_EVENT,
        KNOWLEDGE_WRITE_INTENT_EVENT,
        KNOWLEDGE_WRITE_COMPLETED_EVENT,
        KNOWLEDGE_WRITE_NOOP_EVENT,
        KNOWLEDGE_WRITE_FAILED_EVENT,
    }
)


# Fehler des Stores, die nachweislich vor jeder Ersetzung einer Datei entstehen:
# eine Eingabe- oder Pfadverletzung kann keine Datei veraendert haben. Jeder
# andere Fehler kann nach ``os.replace`` auftreten und laesst den Dateizustand
# deshalb ungeklaert.
_PRE_WRITE_ERRORS = (KnowledgeContentError, UnsafeKnowledgePathError)


# Was der Audit-Zustand ueber den Dateizustand aussagt.
OUTCOME_NOT_WRITTEN = "not_written"


OUTCOME_UNKNOWN = "unknown"


OUTCOME_PARTIAL_STRUCTURE = "partial_structure_possible"


_NO_AUTOMATIC_RETRY = (
    "Der Vorgang wird nicht automatisch wiederholt; die Datei wird nicht "
    "zurueckgeloescht. Bitte den Dateiinhalt pruefen und nur bewusst erneut "
    "ausfuehren."
)


class ContextKnowledgeError(DomainError):
    """Basis fuer Fehler der Context/Knowledge-Verbindung."""


class ContextKnowledgeNotInitializedError(ContextKnowledgeError):
    """Der Context hat keinen vollstaendig initialisierten Wissensspeicher."""


class ContextKnowledgeAmbiguousError(ContextKnowledgeError):
    """Der Zustand nach einem Schreibvorgang ist nicht beweisbar.

    Die Dateiaenderung ist erfolgt, der abschliessende DB-Commit nicht -- oder
    die Datei weicht nachweislich vom erwarteten Ergebnis ab, oder der Fehler
    kam nach der Ersetzung.

    Der Audit-Zustand ist damit ungeklaert: ein Intent-Ereignis existiert, kein
    bestaetigender Abschluss. Die Datei wird bewusst *nicht* zurueckgeloescht --
    ein Rollback koennte zuvor manuell bearbeitetes Wissen vernichten.
    """


class _VerificationFailed(Exception):
    """Der persistente Dateizustand weicht vom erwarteten Ergebnis ab.

    Interner Signalfehler: er wird nie nach aussen gegeben, sondern in einen
    Intent + FAILED Audit-Zustand und einen :class:`ContextKnowledgeAmbiguousError`
    uebersetzt.
    """


@dataclass(frozen=True)
class ContextKnowledgeResult:
    """Ergebnis von ``ensure_context_knowledge``."""

    context_id: str
    knowledge_path: str
    operation_id: str
    bound_now: bool
    directory_created: bool
    created_files: tuple[str, ...]
    changed: bool


@dataclass(frozen=True)
class ContextKnowledgeWrite:
    """Ergebnis von ``apply``; enthaelt nur Pfadrelativ, Hash und operation_id."""

    context_id: str
    knowledge_path: str
    operation_id: str
    file: str
    changed: bool
    content_hash: str


@dataclass(frozen=True)
class _ContextBinding:
    """Verbindlicher DB-Stand eines Contexts, ohne Identitaetskarte."""

    context_id: str
    title: str
    knowledge_path: str | None
