"""Controlled qualitative Markdown changes preserving unrelated sections."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from .contracts import (
    CORE_FILES,
    DIRECTORY_MODE,
    KnowledgeContentError,
    KnowledgeContext,
    KnowledgeWrite,
    MAX_SECTION_ENTRIES,
    NOTES_FILE,
    OVERVIEW_FILE,
    PROGRESS_FILE,
    TOPICS_DIR,
)
from .document import (
    MarkdownCodec,
    _Document,
    _check_heading,
    _ends_with_block,
    _history_entry,
    _history_facts,
    _last_history_facts,
    _now,
    _reject_ambiguous_structure,
    _sanitize_text,
    _status_lines,
    _status_semantics,
    _timestamp,
    _timestamped_status,
    parts_title,
)
from .filesystem import KnowledgeFilesystem


class KnowledgeUpdates:
    """Apply Markdown changes using the same filesystem adapter and writer lock."""

    def __init__(self, files: KnowledgeFilesystem) -> None:
        self._files = files

    def ensure_context_knowledge(
        self, context_path: str, *, title: str | None = None, overview: str | None = None
    ) -> KnowledgeContext:
        """Create the core structure of a context without touching existing content."""
        parts = self._files.relative_parts(context_path)
        heading = (title or parts[-1]).strip()
        with self._files._lock:
            directory = self._files.resolve(parts)
            existed = directory.is_dir()
            self._files.guard(directory, expect="any")
            directory.mkdir(mode=DIRECTORY_MODE, parents=True, exist_ok=True)
            self._files.guard(directory, expect="dir")
            topics = self._files.guard(directory / TOPICS_DIR, expect="any")
            if not topics.exists():
                topics.mkdir(mode=DIRECTORY_MODE)
            self._files.guard(topics, expect="dir")
            created_files: list[Path] = []
            for name, initial in (
                (
                    OVERVIEW_FILE,
                    MarkdownCodec._document(
                        heading, [("Ueberblick", overview or "Noch nicht beschrieben.")]
                    ),
                ),
                (PROGRESS_FILE, MarkdownCodec._progress_document()),
                (NOTES_FILE, MarkdownCodec._document(heading, [])),
            ):
                path = self._files.guard_context_file(directory, name)
                if not path.exists():
                    self._files.atomic_write(path, initial)
                    created_files.append(path)
            return KnowledgeContext(
                context_path="/".join(parts),
                directory=directory,
                files=tuple(directory / name for name in CORE_FILES),
                created=not existed,
                created_files=tuple(created_files),
            )

    def update_overview(
        self, context_path: str, text: str, *, heading: str = "Ueberblick"
    ) -> KnowledgeWrite:
        """Replace (or extend) one section of ``overview.md``; all other content stays."""
        body = _sanitize_text(text, field="text")
        if not body.strip():
            raise KnowledgeContentError("overview braucht nicht-leeren Text")
        _reject_ambiguous_structure(body)
        heading = _check_heading(heading)
        with self._files._lock:
            parts = self._files.relative_parts(context_path)
            path = self._files.guard_context_file(self._files.resolve(parts), OVERVIEW_FILE)
            document = MarkdownCodec._parse(
                self._files.read_or_default(path, "")
            ).require_editable()
            if not document.preamble:
                document = _Document(
                    parts_title(context_path), f"# {parts_title(context_path)}", document.sections
                )
            return self._files.write(path, document.with_body(heading, body).render())

    def update_progress(
        self,
        context_path: str,
        *,
        text: str | None = None,
        status: str | None = None,
        percent: int | None = None,
        recorded_at: datetime | None = None,
    ) -> KnowledgeWrite:
        """Update ``progress.md``: status block, free text and a verifiable history entry.

        Repeating the same update is a no-op: an identical history entry is never
        appended twice and a semantically unchanged status does not rewrite the file
        just because the timestamp would change.
        """
        if text is None and status is None and percent is None:
            raise KnowledgeContentError(
                "update_progress braucht mindestens text, status oder percent"
            )
        if percent is not None and not 0 <= percent <= 100:
            raise KnowledgeContentError("percent muss zwischen 0 und 100 liegen")
        body = _sanitize_text(text, field="text") if text is not None else None
        if body is not None:
            _reject_ambiguous_structure(body)
        moment = recorded_at or _now()
        with self._files._lock:
            parts = self._files.relative_parts(context_path)
            path = self._files.guard_context_file(self._files.resolve(parts), PROGRESS_FILE)
            document = MarkdownCodec._parse(
                self._files.read_or_default(path, "")
            ).require_editable()
            if not document.preamble:
                document = _Document("Fortschritt", "# Fortschritt", document.sections)
            if not document.sections:
                # Keep the preamble; only add the managed sections.
                document = _Document(
                    document.title,
                    document.preamble,
                    [("Status", "- Status: unbekannt"), ("Fortschritt", "")],
                )
            status_lines = _status_lines(document.body_of("Status"), status, percent)
            progress_body = body if body is not None else document.body_of("Fortschritt")
            if not progress_body:
                progress_body = "Noch kein Fortschritt dokumentiert."

            entry = _history_entry(body, status, percent, moment)
            # Compare the history entry without its timestamp: the same update retried
            # a minute later must not append a second, differently stamped line.
            entry_facts = _history_facts(body, status, percent)
            already_recorded = _last_history_facts(document.body_of("Verlauf")) == entry_facts
            status_unchanged = _status_semantics(
                document.body_of("Status"), status, percent
            ) == _status_semantics(document.body_of("Status"), None, None)
            if (
                status_unchanged
                and progress_body == document.body_of("Fortschritt")
                and already_recorded
            ):
                current = self._files.read_or_default(path, "")
                return KnowledgeWrite(path=path, content=current, created=False, changed=False)

            document = document.with_body_first("Status", _timestamped_status(status_lines, moment))
            document = document.with_body("Fortschritt", progress_body)
            if not already_recorded:
                document = document.with_body("Verlauf", entry, append=True)
            return self._files.write(path, document.render())

    def append_note(
        self,
        context_path: str,
        text: str,
        *,
        heading: str | None = None,
        recorded_at: datetime | None = None,
    ) -> KnowledgeWrite:
        """Append a new dated section to ``notes.md``; existing notes are never rewritten.

        This is a real append: repeating the same text is allowed and creates a
        second entry, so blind retries are not safe here.
        """
        body = _sanitize_text(text, field="text")
        if not body.strip():
            raise KnowledgeContentError("Note braucht nicht-leeren Text")
        _reject_ambiguous_structure(body)
        moment = recorded_at or _now()
        section_heading = (
            _check_heading(heading) if heading and heading.strip() else _timestamp(moment)
        )
        with self._files._lock:
            parts = self._files.relative_parts(context_path)
            path = self._files.guard_context_file(self._files.resolve(parts), NOTES_FILE)
            document = MarkdownCodec._parse(
                self._files.read_or_default(path, "")
            ).require_editable()
            if not document.preamble:
                document = _Document(
                    parts_title(context_path), f"# {parts_title(context_path)}", document.sections
                )
            if len(document.sections) >= MAX_SECTION_ENTRIES:
                raise KnowledgeContentError("notes.md hat zu viele Eintraege")
            sections = [*document.sections, (section_heading, body)]
            return self._files.write(
                path, _Document(document.title, document.preamble, sections).render()
            )

    def create_topic(
        self, context_path: str, topic: str, *, text: str | None = None, status: str | None = None
    ) -> KnowledgeWrite:
        """Create a topic file; refuses to overwrite an existing topic."""
        body = _sanitize_text(text, field="text") if text is not None else None
        if body is not None:
            _reject_ambiguous_structure(body)
        with self._files._lock:
            path = self._files.guard(self._files.topic_path(context_path, topic), expect="any")
            if path.exists():
                raise KnowledgeContentError(f"Topic existiert bereits: {path.name}")
            sections: list[tuple[str, str]] = [
                ("Status", _timestamped_status(_status_lines("", status, None), _now())),
                ("Inhalt", body or "Noch keine Inhalte dokumentiert."),
            ]
            return self._files.write(path, _Document(topic, f"# {topic}", sections).render())

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
        """Update an existing topic file, or create it when it is missing yet.

        ``append`` is on by default so an update never silently drops knowledge
        that is already in the file. The two modes are strictly separate:

        * Append adds ``text`` as one block. Repeating the *same* block right
          after it was appended is a no-op, and the whole block is compared, so
          a multi-paragraph text is not duplicated by a retry.
        * Replace makes ``text`` the complete new section body. It is a no-op
          only if the section already consists of exactly this text, never
          because the text happens to be the last paragraph of a longer section.
        """
        body = _sanitize_text(text, field="text") if text is not None else None
        if body is not None:
            _reject_ambiguous_structure(body)
        if body is None and status is None and percent is None:
            raise KnowledgeContentError("update_topic braucht mindestens text, status oder percent")
        if percent is not None and not 0 <= percent <= 100:
            raise KnowledgeContentError("percent muss zwischen 0 und 100 liegen")
        moment = recorded_at or _now()
        with self._files._lock:
            path = self._files.guard(self._files.topic_path(context_path, topic), expect="any")
            exists = path.exists()
            document = MarkdownCodec._parse(
                self._files.read_or_default(path, "")
            ).require_editable()
            document_title = document.title or topic
            if not document.preamble:
                document = _Document(document_title, f"# {document_title}", document.sections)
            if not document.sections:
                document = _Document(
                    document.title,
                    document.preamble,
                    [("Inhalt", "Noch keine Inhalte dokumentiert.")],
                )

            current_status = document.body_of("Status")
            status_unchanged = _status_semantics(
                current_status, status, percent
            ) == _status_semantics(current_status, None, None)
            current_content = document.body_of("Inhalt").strip()
            if body is not None:
                incoming = body.strip()
                if not incoming:
                    raise KnowledgeContentError("update_topic braucht nicht-leeren Text")
                if append:
                    # Repeating the identical block must not duplicate it, neither
                    # as a whole-document write nor as a second appended block.
                    already_present = _ends_with_block(current_content, incoming)
                else:
                    # Replace is a no-op only if the target section already *is*
                    # this text; a matching last paragraph still has to be replaced.
                    already_present = current_content == incoming
            else:
                already_present = True
            if exists and status_unchanged and already_present:
                current = self._files.read_or_default(path, "")
                return KnowledgeWrite(path=path, content=current, created=False, changed=False)

            document = document.with_body_first(
                "Status",
                _timestamped_status(_status_lines(current_status, status, percent), moment),
            )
            if body is not None and not already_present:
                document = document.with_body("Inhalt", body, append=append)
            return self._files.write(path, document.render())

    def list_topics(self, context_path: str) -> tuple[str, ...]:
        """Names of the topic files of one context, sorted and path-safe.

        Every candidate entry is checked before it is used, with the same
        no-symlink policy as :meth:`read` and the write path: a symlinked or
        otherwise irregular entry is refused instead of being followed. A
        subdirectory inside ``topics/`` is simply not a topic file.
        """
        with self._files._lock:
            directory = self._files.guard(
                self._files.resolve(self._files.relative_parts(context_path)) / TOPICS_DIR,
                expect="any",
            )
            if not directory.exists():
                return ()
            self._files.guard(directory, expect="dir")
            stems: list[str] = []
            for item in directory.glob("*.md"):
                self._files.guard(item, expect="any")
                if item.is_dir():
                    continue
                stems.append(item.stem)
            return tuple(sorted(stems))
