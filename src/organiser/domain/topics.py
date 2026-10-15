"""Pure topic vocabulary: title normalisation, status set and knowledge keys.

This module is deliberately free of database, filesystem and API dependencies. It
decides only three things:

* what the *display* title of a topic is,
* what its ``title_key`` (the duplicate detection key) is,
* what its immutable ``knowledge_key`` is and which status values exist.

Everything a caller could use to *address* a topic file is absent on purpose: a
path, a filename or a store location is never expressible here. The physical
address is derived from the database key alone (see
:mod:`organiser.services.topic_knowledge`).

Normalisation rules (``title_key``)
-----------------------------------

``UNIQUE(context_id, title_key)`` is the only duplicate guard a new topic has, so
the key must fold exactly the spellings a user considers "the same topic" while
still keeping genuinely different topics apart:

1. ``NFKC`` -- compatibility composition, so ``"Bilanzierung"`` written with a
   fullwidth or composed ligature form folds onto its plain spelling.
2. ``casefold`` -- stronger and locale-independent than ``lower``; ``"STRASSE"``
   and ``"straße"`` meet.
3. Whitespace collapsed to single spaces and trimmed -- so ``"Bilanz   Teil"``
   and ``" Bilanz Teil "`` are one topic and the stored key never has edge
   whitespace.

Collapsing whitespace means a key never contains a newline, so the value stays
usable inside a URL query or a JSON payload without further escaping.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from organiser.domain.workspace_contracts import TopicStatus


# The four coarse statuses of this slice. The database CHECK constraint is the
# last guarantee; this tuple exists so a bad value is refused before an
# IntegrityError.
TOPIC_STATUSES: tuple[str, ...] = tuple(status.value for status in TopicStatus)

# A topic is a persistent subject, not a path. The limit keeps the display title
# usable as the semantic title inside a markdown file header and matches the
# ``KnowledgeUpdate.topic`` bound.
MAX_TOPIC_TITLE_LENGTH = 200

KNOWLEDGE_KEY_PREFIX = "topic-"

_WHITESPACE = re.compile(r"\s+")


class TopicInputError(ValueError):
    """A topic value cannot be turned into a valid row.

    Carries no database, filesystem or API meaning; callers translate it into
    their own error family (``DomainValidationError`` for the services).
    """


def normalize_title(title: str) -> str:
    """Return the display title: trimmed, whitespace-collapsed, length-checked.

    The display title is what a human reads and what a topic file's ``# heading``
    may mention. It is *not* the duplicate key: ``normalize_title_key`` folds it
    further for comparison.
    """
    if not isinstance(title, str):
        raise TopicInputError("title muss ein String sein")
    collapsed = _WHITESPACE.sub(" ", title).strip()
    if not collapsed:
        raise TopicInputError("title darf nicht leer sein")
    if len(collapsed) > MAX_TOPIC_TITLE_LENGTH:
        raise TopicInputError(f"title darf maximal {MAX_TOPIC_TITLE_LENGTH} Zeichen lang sein")
    # A control character would break the markdown header this title is written
    # into and cannot survive a database round trip predictably.
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in collapsed):
        raise TopicInputError("title darf keine Steuerzeichen enthalten")
    return collapsed


def normalize_title_key(title: str) -> str:
    """Return the duplicate-detection key for ``title``.

    See the module docstring for the three folding steps. The result is never
    empty: a title that folds away entirely (for example a string of only
    zero-width or combining characters) is refused rather than stored as ``""``,
    which the ``CHECK(length(title_key) > 0)`` would reject with a worse message.
    """
    key = unicodedata.normalize("NFKC", normalize_title(title)).casefold()
    key = _WHITESPACE.sub(" ", key).strip()
    if not key:
        raise TopicInputError("title ergibt keinen verwendbaren title_key")
    return key


def knowledge_key_for(topic_id: str) -> str:
    """Return the immutable knowledge key of a topic.

    The key is derived purely from the identifier: ``topic-<id>``. It therefore
    survives a rename unchanged, and two contexts can never collide because the
    id is globally unique. The result matches the database constraint
    ``CHECK(knowledge_key = 'topic-' || id)`` exactly.
    """
    if not isinstance(topic_id, str) or not topic_id.strip():
        raise TopicInputError("topic_id darf nicht leer sein")
    identifier = topic_id.strip()
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in identifier):
        raise TopicInputError("topic_id darf keine Steuerzeichen enthalten")
    if "/" in identifier or "\\" in identifier or identifier in {".", ".."}:
        raise TopicInputError("topic_id darf kein Pfad sein")
    return f"{KNOWLEDGE_KEY_PREFIX}{identifier}"


def validate_topic_status(status: str) -> str:
    """Return ``status`` if it is one of the four coarse values."""
    if not isinstance(status, str) or status not in TOPIC_STATUSES:
        raise TopicInputError(f"status muss einer von {', '.join(TOPIC_STATUSES)} sein")
    return status


@dataclass(frozen=True)
class TopicBinding:
    """The database-owned facts a topic file write is allowed to rely on.

    This is the *only* input a topic knowledge write receives besides the caller's
    own text. It carries the semantic title (for the file header) and the
    knowledge key (for the physical address) but no path, no filename and no
    caller-supplied location: the address is computed from ``knowledge_key``
    alone.
    """

    topic_id: str
    context_id: str
    title: str
    knowledge_key: str
