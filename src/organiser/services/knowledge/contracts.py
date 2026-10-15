"""Store results, failure types and fixed Markdown/file limits."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from organiser.domain.knowledge_path import (
    MAX_KNOWLEDGE_PATH_LENGTH,
)


OVERVIEW_FILE = "overview.md"


PROGRESS_FILE = "progress.md"


NOTES_FILE = "notes.md"


TOPICS_DIR = "topics"


CORE_FILES = (OVERVIEW_FILE, PROGRESS_FILE, NOTES_FILE)


DIRECTORY_MODE = 0o700


FILE_MODE = 0o600


# Re-exportiert, damit bestehende Importe stabil bleiben; die Zahl selbst ist in
# `organiser.domain.knowledge_path` definiert, damit Store und Domain dieselbe Grenze teilen.
MAX_PATH_LENGTH = MAX_KNOWLEDGE_PATH_LENGTH


MAX_TEXT_LENGTH = 20_000


MAX_SECTION_ENTRIES = 200


MAX_TOPIC_NAME_LENGTH = 80


# Grenzen des bounded, seiteneffektfreien Readers. Sie sind feste Obergrenzen und
# ueber keinen Aufruferwert erhoehbar: `read_excerpt` klemmt `max_bytes` hierauf
# fest. Ohne diese Grenze wuerde ein Read-Modell eine beliebig grosse Datei in ein
# 32k-Zeichen-Packet ziehen muessen.
MAX_EXCERPT_BYTES = 256 * 1024


# Harte Obergrenze fuer die Aufzaehlung von Topic-Dateinamen: hoechstens so viele
# Namen werden aus dem Verzeichnis gelesen, egal wie viele dort liegen. Die
# Auswahl ist zusaetzlich durch `limit` kleiner. Ohne diese Grenze koennte ein
# fremdes, sehr grosses Verzeichnis den Read eines Contexts beliebig teuer machen.
MAX_TOPIC_FILE_SCAN = 2_000


_UNSAFE_SEGMENT = re.compile(r"[\x00-\x1f\x7f]")


_SECTION_HEADING = re.compile(r"^##[ \t]+(?P<title>\S.*?)[ \t]*$")


_DOCUMENT_TITLE = re.compile(r"^#[ \t]+(?P<title>\S.*?)[ \t]*$")


_FENCE = re.compile(r"^[ \t]{0,3}(?P<fence>`{3,}|~{3,})")


_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


# Sections the store manages itself; a duplicate occurrence is ambiguous.
CORE_HEADINGS = ("Ueberblick", "Status", "Fortschritt", "Verlauf", "Inhalt", "Notizen")


class KnowledgeError(Exception):
    """Base class for all knowledge store failures."""


class UnsafeKnowledgePathError(KnowledgeError):
    """A supplied path would leave the knowledge root."""


class KnowledgeContentError(KnowledgeError):
    """Supplied content cannot be written as requested."""


class KnowledgeNotFoundError(KnowledgeError):
    """A requested knowledge file does not exist."""


class KnowledgeExcerptTooLargeError(KnowledgeError):
    """A file exceeds the bounded reader's hard size limit and is therefore skipped.

    Bewusst kein Teilfehler: der Aufrufer ueberspringt die Datei sichtbar und
    meldet eine Warnung, statt auf ein Fragment ohne vollstaendigen Hash zu
    erhalten. Ein Hash waere ohnehin nur ein Hash des Fragments.
    """


class KnowledgeUnstableError(KnowledgeError):
    """A file changed while it was being read; the excerpt is not a consistent snapshot."""


@dataclass(frozen=True)
class KnowledgeExcerptRead:
    """Bounded, read-only view of one knowledge file.

    ``content_hash`` ist immer der Hash der *vollstaendigen* Dateibytes. Das ist
    nur zulaessig, weil ``read_excerpt`` die Groessengrenze vor dem Lesen prueft
    und die Datei nach dem Lesen gegen dieselbe Grenze und gegen ihre Identitaet
    verifiziert. Ein Excerpt ohne diesen Hash waere fuer einen spaeteren
    Aenderungsvergleich unbrauchbar.
    """

    relative_path: str
    text: str
    truncated: bool
    content_hash: str
    byte_size: int
    char_length: int


@dataclass(frozen=True)
class KnowledgeContext:
    """Result of ensuring the core structure of one context."""

    context_path: str
    directory: Path
    files: tuple[Path, ...]
    created: bool
    created_files: tuple[Path, ...]

    @property
    def overview(self) -> Path:
        return self.directory / OVERVIEW_FILE

    @property
    def progress(self) -> Path:
        return self.directory / PROGRESS_FILE

    @property
    def notes(self) -> Path:
        return self.directory / NOTES_FILE

    @property
    def topics_directory(self) -> Path:
        return self.directory / TOPICS_DIR


@dataclass(frozen=True)
class KnowledgeWrite:
    """Result of a single write operation."""

    path: Path
    content: str
    created: bool
    changed: bool


_HISTORY_ENTRY = re.compile(r"^-\s+(?P<when>\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC)\s*(?P<facts>.*)$")
