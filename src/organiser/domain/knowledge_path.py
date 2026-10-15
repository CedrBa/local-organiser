"""Gemeinsame, reine Pfadvalidierung fuer Wissensverweise aus der Datenbank.

Dieses Modul ist die einzige Stelle, die entscheidet, ob eine Zeichenkette ein
kanonischer, relativer POSIX-Pfad unterhalb des Knowledge Roots ist. Beide
Aufrufer -- Context-Domain und KnowledgeStore -- delegieren an dieselben Regeln,
damit ein im Context akzeptierter Wert immer auch im Store gueltig ist und
umgekehrt.

Bewusste Grenzen:

* Keine Datenbank-, Dateisystem- oder Settings-Abhangigkeit. Die Funktion liest
  nichts und schreibt nichts; sie ist eine reine Syntaxpruefung.
* Keine stillschweigende Normalisierung. Jede mehrdeutige Schreibweise wird
  abgelehnt, damit derselbe Speicherort nicht unter zwei Namen entstehen kann.
* `None` bleibt ein gueltiger Eingang: `NULL` bedeutet in der Datenbank
  "noch nicht gebunden", nicht "ungueltig".
"""

from __future__ import annotations

import re
from pathlib import PureWindowsPath


MAX_KNOWLEDGE_PATH_LENGTH = 512

# Vom Store verwaltete Namen. Ein Context-Pfad darf sie nicht als Segment
# benutzen, sonst zeigt er auf die Struktur des Stores statt auf einen Context.
RESERVED_CONTEXT_SEGMENTS = frozenset({"topics", "overview.md", "progress.md", "notes.md"})

# Segment, das wie ein Kommandozeilen-Option aussieht; der Store lehnt es ab.
_FORBIDDEN_SEGMENT_PREFIX = "-"

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


class KnowledgePathError(ValueError):
    """Rein sprachlicher Vertragsbruch in einem Wissenspfad.

    Bewusst ohne Bezug zu DB, Store oder API: die Aufrufer uebersetzen in ihre
    eigene Fehlerfamilie (`DomainValidationError` bzw. `UnsafeKnowledgePathError`).
    """


def validate_knowledge_path(
    value: str,
    *,
    field: str = "knowledge_path",
    allow_reserved_segments: bool = False,
) -> str:
    """Pruefe einen relativen, kanonischen POSIX-Wissenspfad.

    Erlaubt sind ausschliesslich relative POSIX-Pfade aus maximal
    ``MAX_KNOWLEDGE_PATH_LENGTH`` Zeichen. Abgelehnt werden absolute POSIX- und
    Windows-Pfade, Laufwerkspfade, Backslashes, Steuerzeichen, leere Segmente,
    ``.``/``..``, Segmente mit Randleerzeichen sowie Segmente, die mit ``-``
    beginnen. Mit ``allow_reserved_segments=False`` zusaetzlich die vom Store
    reservierten Segmentnamen.

    Jeder Praefix aus einem Zeichen und Doppelpunkt (auch ``1:2``) gilt wie
    ``C:relative`` als Laufwerkspfad und wird abgelehnt. Diese Policy entspricht
    der eingefrorenen Migration 0009 und ist unabhaengig von der Python-Version:
    ``PureWindowsPath.drive`` erkennt solche Praefixe je nach Version anders.
    Doppelpunkte an anderen Positionen (``ab:cd``, ``a/b:c``) bleiben erlaubt.

    Rueckgabewert ist der unveraenderte Eingabewert: die Funktion normalisiert
    bewusst nichts, damit der Aufrufer genau den Pfad zurueckhaelt, den er
    angefordert hat.
    """
    if not isinstance(value, str):
        raise KnowledgePathError(f"{field} muss ein String sein")
    if not value:
        raise KnowledgePathError(f"{field} darf nicht leer sein")
    if len(value) > MAX_KNOWLEDGE_PATH_LENGTH:
        raise KnowledgePathError(
            f"{field} darf maximal {MAX_KNOWLEDGE_PATH_LENGTH} Zeichen lang sein"
        )
    if value != value.strip():
        raise KnowledgePathError(f"{field} darf nicht mit Whitespace beginnen oder enden")
    if "\\" in value:
        raise KnowledgePathError(f"{field} muss mit / getrennt sein")
    if _CONTROL_CHARACTERS.search(value):
        raise KnowledgePathError(f"{field} darf keine Steuerzeichen enthalten")
    if value.startswith("/") or PureWindowsPath(value).is_absolute():
        raise KnowledgePathError(f"{field} muss relativ sein")
    if value[1:2] == ":" or PureWindowsPath(value).drive:
        raise KnowledgePathError(f"{field} darf kein Windows-Laufwerkspfad sein")
    if value.endswith("/"):
        raise KnowledgePathError(f"{field} darf nicht mit '/' enden")

    for segment in value.split("/"):
        if not segment:
            raise KnowledgePathError(f"{field} darf keine leeren Segmente enthalten")
        if segment != segment.strip():
            raise KnowledgePathError(f"{field} darf keine Segmente mit Randleerzeichen enthalten")
        if segment in {".", ".."}:
            raise KnowledgePathError(f"{field} darf keine '.'- oder '..'-Segmente enthalten")
        if segment.startswith(_FORBIDDEN_SEGMENT_PREFIX):
            raise KnowledgePathError(f"{field} darf kein Segment mit '-' am Anfang enthalten")
        if not allow_reserved_segments and segment.casefold() in RESERVED_CONTEXT_SEGMENTS:
            raise KnowledgePathError(
                f"{field} darf die reservierten Store-Segmente "
                f"({', '.join(sorted(RESERVED_CONTEXT_SEGMENTS))}) nicht verwenden"
            )
    return value


def validate_context_path(value: str, *, field: str = "knowledge_path") -> str:
    """Pruefe einen Context-Pfad; reservierte Store-Segmente sind verboten."""
    return validate_knowledge_path(value, field=field, allow_reserved_segments=False)


def default_knowledge_path(context_id: str) -> str:
    """Kanonischer Default-Pfad eines Contexts: ``contexts/<context-id>``.

    Der Pfad haengt ausschliesslich an der Context-ID. Damit bleibt er bei
    Umbenennung eines Titels und beim Reparenting stabil.
    """
    if not isinstance(context_id, str) or not context_id.strip():
        raise KnowledgePathError("context_id darf nicht leer sein")
    return validate_context_path(f"contexts/{context_id}", field="context_id")
