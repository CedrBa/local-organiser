"""Stable knowledge facade composing filesystem, reader and content operations."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from organiser.core.config import Settings, get_settings
from organiser.domain.knowledge import KnowledgeUpdate
from .contracts import KnowledgeContext, KnowledgeExcerptRead, KnowledgeWrite, MAX_EXCERPT_BYTES
from .document import MarkdownCodec
from .filesystem import KnowledgeFilesystem
from .reader import KnowledgeReader
from .updates import KnowledgeUpdates


class KnowledgeStore:
    """Coordinate Markdown operations through one shared filesystem and lock.

    Readers and updates share the adapter. Locks are process-local; callers must
    run a single writer process. Individual file replacement is atomic, but this
    store does not provide a joint database/filesystem transaction.
    """

    def __init__(self, root: Path | str) -> None:
        self._filesystem = KnowledgeFilesystem(root)
        self._reader = KnowledgeReader(self._filesystem)
        self._updates = KnowledgeUpdates(self._filesystem)
        self._lock = self._filesystem._lock

    @property
    def root(self) -> Path:
        """Return the resolved root without creating it."""
        return self._filesystem.root

    def _root_ready(self) -> Path:
        """Create the root on demand; ``__init__`` already resolved it to a real path."""
        return self._filesystem.root_ready()

    _relative_parts = staticmethod(KnowledgeFilesystem.relative_parts)

    def _resolve(self, parts: tuple[str, ...]) -> Path:
        """Join validated segments below the root and prove the result stays inside."""
        return self._filesystem.resolve(parts)

    def context_directory(self, context_path: str) -> Path:
        """Public, path-safe accessor for the directory of one context."""
        return self._filesystem.context_directory(context_path)

    def topic_path(self, context_path: str, topic: str) -> Path:
        """Public, path-safe accessor for the markdown file of one topic."""
        return self._filesystem.topic_path(context_path, topic)

    def ensure_context_knowledge(
        self, context_path: str, *, title: str | None = None, overview: str | None = None
    ) -> KnowledgeContext:
        """Create the core structure of a context without touching existing content."""
        return self._updates.ensure_context_knowledge(context_path, title=title, overview=overview)

    _document = staticmethod(MarkdownCodec._document)

    _progress_document = staticmethod(MarkdownCodec._progress_document)

    _split_fences = staticmethod(MarkdownCodec._split_fences)

    _parse = staticmethod(MarkdownCodec._parse)

    _atomic_write = staticmethod(KnowledgeFilesystem.atomic_write)

    def _guard(self, path: Path, *, expect: str) -> Path:
        """Approve a path before any access, or raise."""
        return self._filesystem.guard(path, expect=expect)

    def _guard_context_file(self, directory: Path, name: str) -> Path:
        """Approve ``directory / name`` and require it to be a real file if present."""
        return self._filesystem.guard_context_file(directory, name)

    def _write(self, path: Path, content: str) -> KnowledgeWrite:
        """Delegate write to the shared filesystem component."""
        return self._filesystem.write(path, content)

    def _read_guarded(self, path: Path) -> str | None:
        """Read a knowledge file, refusing anything that is not a regular file."""
        return self._filesystem.read_guarded(path)

    def _read(self, path: Path, default: str) -> str:
        """Delegate read to the shared filesystem component."""
        return self._filesystem.read_or_default(path, default)

    def update_overview(
        self, context_path: str, text: str, *, heading: str = "Ueberblick"
    ) -> KnowledgeWrite:
        """Replace (or extend) one section of ``overview.md``; all other content stays."""
        return self._updates.update_overview(context_path, text, heading=heading)

    def update_progress(
        self,
        context_path: str,
        *,
        text: str | None = None,
        status: str | None = None,
        percent: int | None = None,
        recorded_at: datetime | None = None,
    ) -> KnowledgeWrite:
        """Update ``progress.md``: status block, free text and a verifiable history entry."""
        return self._updates.update_progress(
            context_path, text=text, status=status, percent=percent, recorded_at=recorded_at
        )

    def append_note(
        self,
        context_path: str,
        text: str,
        *,
        heading: str | None = None,
        recorded_at: datetime | None = None,
    ) -> KnowledgeWrite:
        """Append a new dated section to ``notes.md``; existing notes are never rewritten."""
        return self._updates.append_note(
            context_path, text, heading=heading, recorded_at=recorded_at
        )

    def create_topic(
        self, context_path: str, topic: str, *, text: str | None = None, status: str | None = None
    ) -> KnowledgeWrite:
        """Create a topic file; refuses to overwrite an existing topic."""
        return self._updates.create_topic(context_path, topic, text=text, status=status)

    def update_topic(
        self,
        context_path: str,
        topic: str,
        *,
        text: str | None = None,
        status: str | None = None,
        percent: int | None = None,
        append: bool = True,
        recorded_at: datetime | None = None,
    ) -> KnowledgeWrite:
        """Update an existing topic file, or create it when it is missing yet."""
        return self._updates.update_topic(
            context_path,
            topic,
            text=text,
            status=status,
            percent=percent,
            append=append,
            recorded_at=recorded_at,
        )

    def list_topics(self, context_path: str) -> tuple[str, ...]:
        """Names of the topic files of one context, sorted and path-safe."""
        return self._updates.list_topics(context_path)

    def read(self, relative_path: str) -> str:
        """Read a markdown file below the knowledge root; used for context packets."""
        return self._filesystem.read(relative_path)

    def apply(self, update: KnowledgeUpdate) -> KnowledgeWrite:
        """Apply one validated store update; the caller enforces application scope."""
        if update.action == "update_overview":
            return self.update_overview(
                update.context_path, update.text or "", heading=update.heading or "Ueberblick"
            )
        if update.action == "update_progress":
            return self.update_progress(
                update.context_path,
                text=update.text,
                status=update.status,
                percent=update.percent,
                recorded_at=update.recorded_at,
            )
        if update.action == "append_note":
            return self.append_note(
                update.context_path,
                update.text or "",
                heading=update.heading,
                recorded_at=update.recorded_at,
            )
        if update.action == "create_topic":
            return self.create_topic(
                update.context_path, update.topic or "", text=update.text, status=update.status
            )
        return self.update_topic(
            update.context_path,
            update.topic or "",
            text=update.text,
            status=update.status,
            percent=update.percent,
            recorded_at=update.recorded_at,
        )

    def _open_below_root(self, parts: tuple[str, ...], *, expect_file: bool) -> int:
        """Open a validated path component by component and return a file descriptor."""
        return self._filesystem.open_below_root(parts, expect_file=expect_file)

    def _resolve_for_read(self, parts: tuple[str, ...]) -> Path:
        """Join validated segments below the root *without creating anything*."""
        return self._filesystem.resolve_for_read(parts)

    def read_excerpt(
        self, relative_path: str, max_chars: int, max_bytes: int = MAX_EXCERPT_BYTES
    ) -> KnowledgeExcerptRead:
        """Read a bounded excerpt plus the complete file hash, without any write."""
        return self._reader.read_excerpt(relative_path, max_chars, max_bytes)

    def list_topic_files(self, context_path: str, *, limit: int = 64) -> tuple[str, ...]:
        """Sorted, bounded, read-only names of the topic files of one context."""
        return self._reader.list_topic_files(context_path, limit=limit)


def get_knowledge_store(settings: Settings | None = None) -> KnowledgeStore:
    """Build a store from the configured knowledge root (``ORGANISER_KNOWLEDGE_ROOT``)."""
    return KnowledgeStore((settings or get_settings()).knowledge_root)
