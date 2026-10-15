"""Explicit topic-knowledge coordination using committed DB identity and one writer lock."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session
from organiser.db.models import Context, Topic
from organiser.domain.topics import TopicBinding, TopicInputError, knowledge_key_for
from organiser.services.context_knowledge import (
    CORE_FILES,
    TOPICS_DIRECTORY,
    ContextKnowledgeError,
    ContextKnowledgeService,
)
from organiser.services.domain import (
    ConflictError,
    EventContext,
    NotFoundError,
    record_event,
)
from organiser.services.knowledge import (
    KnowledgeContentError,
    KnowledgeError,
    KnowledgeStore,
    _ends_with_block,
    _reject_ambiguous_structure,
    _sanitize_text,
)
from .boundary import _application_operation
from .contracts import (
    MAX_TEXT_LENGTH,
    NOTES_SECTION,
    PROGRESS_SECTION,
    TOPIC_KNOWLEDGE_FAILED_EVENT,
    TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_INIT_INTENT_EVENT,
    TOPIC_KNOWLEDGE_INIT_NOOP_EVENT,
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_WRITE_INTENT_EVENT,
    TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT,
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeContentError,
    TopicKnowledgeEnsureResult,
    TopicKnowledgeError,
    TopicKnowledgeNotInitializedError,
    TopicKnowledgeValidationError,
    TopicKnowledgeWrite,
    _CONTENT_DEFECTS,
    _NO_AUTOMATIC_RETRY,
    _PRE_WRITE_ERRORS,
    _ResolvedTopic,
)
from .document import placeholder_document


class TopicKnowledgeService:
    """Coordinate explicit topic knowledge writes through the shared single-process store.

    Owns application transactions and records intent/outcomes; caller work must
    be closed before entry. Database and filesystem writes are not jointly atomic."""

    def __init__(self, session: Session, store: KnowledgeStore) -> None:
        self._session = session
        self._store = store

    @property
    def store(self) -> KnowledgeStore:
        return self._store

    def _require_application_boundary(self) -> None:
        if self._session.info.get("defer_domain_commit"):
            raise TopicKnowledgeError(
                "Dateisystemarbeit ist in einer defer_domain_commit-Transaktion nicht erlaubt"
            )
        if (
            self._session.in_transaction()
            or self._session.new
            or self._session.dirty
            or self._session.deleted
        ):
            raise TopicKnowledgeError(
                "Der Topic-Knowledge-Service benoetigt eine saubere eigene Application-Transaktion"
            )
        if isinstance(self._session.get_bind(), Connection):
            raise TopicKnowledgeError(
                "Eine an eine fremde Connection gebundene Session ist nicht erlaubt"
            )

    def _load_binding(self, topic_id: str) -> _ResolvedTopic:
        if not isinstance(topic_id, str) or not topic_id.strip():
            raise TopicKnowledgeValidationError("topic_id darf nicht leer sein")
        row = self._session.execute(
            select(
                Topic.id,
                Topic.context_id,
                Topic.title,
                Topic.knowledge_key,
                Context.knowledge_path,
            )
            .join(Context, Context.id == Topic.context_id)
            .where(Topic.id == topic_id)
        ).one_or_none()
        if row is None:
            raise NotFoundError("Topic nicht gefunden")
        try:
            if row.knowledge_key != knowledge_key_for(row.id):
                raise TopicInputError("Ungueltige Topic-Knowledge-Bindung")
        except TopicInputError as error:
            raise TopicKnowledgeError("Ungueltige Topic-Knowledge-Bindung") from error
        resolved = _ResolvedTopic(
            TopicBinding(row.id, row.context_id, row.title, row.knowledge_key), row.knowledge_path
        )
        # Entry proved there was no caller work; this ends only our own read.
        # The normal session factory uses expire_on_commit=False.
        self._session.commit()
        return resolved

    @staticmethod
    def _relative_file(knowledge_key: str) -> str:
        return f"{TOPICS_DIRECTORY}/{knowledge_key}.md"

    def _resolve_file(self, knowledge_path: str, knowledge_key: str) -> Path:
        directory = self._store.context_directory(knowledge_path)
        self._store._guard(directory / TOPICS_DIRECTORY, expect="dir")
        path = directory / self._relative_file(knowledge_key)
        self._store._guard(path, expect="file")
        return path

    def _assert_context_ready(self, knowledge_path: str | None) -> str:
        if knowledge_path is None or not self._store.root.is_dir():
            raise TopicKnowledgeNotInitializedError(
                "Context-Knowledge muss zuerst explizit initialisiert werden"
            )
        directory = self._store.context_directory(knowledge_path)
        if not directory.is_dir():
            raise TopicKnowledgeNotInitializedError(
                "Context-Knowledge ist nicht vollstaendig initialisiert"
            )
        for name in CORE_FILES:
            path = directory / name
            self._store._guard(path, expect="file")
            if not path.is_file():
                raise TopicKnowledgeNotInitializedError(
                    "Context-Knowledge ist nicht vollstaendig initialisiert"
                )
        topics = directory / TOPICS_DIRECTORY
        self._store._guard(topics, expect="dir")
        if not topics.is_dir():
            raise TopicKnowledgeNotInitializedError(
                "Context-Knowledge ist nicht vollstaendig initialisiert"
            )
        return knowledge_path

    def _record(
        self,
        event_type: str,
        binding: TopicBinding,
        payload: dict[str, Any],
        context: EventContext | None,
    ) -> None:
        record_event(
            self._session,
            event_type=event_type,
            entity_type="topic",
            entity_id=binding.topic_id,
            payload=payload,
            context=context,
        )
        try:
            self._session.commit()
        except Exception:
            self._session.rollback()
            raise

    def _failure(self, binding, payload, context, *, code: str, outcome: str) -> None:
        try:
            self._record(
                TOPIC_KNOWLEDGE_FAILED_EVENT,
                binding,
                {
                    **payload,
                    "code": code,
                    "outcome": outcome,
                    "failed_at": datetime.now(UTC).isoformat(),
                    "recovery": _NO_AUTOMATIC_RETRY,
                },
                context,
            )
        except Exception:
            # A durable intent without completion remains the recovery signal.
            pass

    def _content_defect(self, binding, payload, context, error) -> TopicKnowledgeContentError:
        """Audit and build the non-transient error for a defective stored file.

        Both defects are found by reading, so ``not_written`` is proven and the
        failure record is written with the same code the API answers with -- D and
        E can therefore recognise the class from the audit log alone.
        """
        self._failure(
            binding, payload, context, code=TopicKnowledgeContentError.code, outcome="not_written"
        )
        return TopicKnowledgeContentError(
            "Die vorhandene Topic-Datei hat einen benutzerfixbaren Inhaltsfehler "
            f"({type(error).__name__}) und wird nicht automatisch ueberschrieben. {_NO_AUTOMATIC_RETRY}"
        )

    def _intent(self, binding, knowledge_path, operation, context, event_type):
        payload = {
            "operation_id": uuid.uuid4().hex,
            "operation": operation,
            "knowledge_path": knowledge_path,
            "knowledge_key": binding.knowledge_key,
            "relative_file": self._relative_file(binding.knowledge_key),
            "attempted_at": datetime.now(UTC).isoformat(),
        }
        try:
            self._record(event_type, binding, payload, context)
        except Exception as error:
            raise TopicKnowledgeError(
                "Intent konnte nicht gespeichert werden; es wurde keine Topic-Datei geschrieben"
            ) from error
        return payload

    def _verify(self, path: Path, expected: str, binding, payload, context) -> str:
        try:
            self._store._guard(path, expect="file")
            persisted_bytes = path.read_bytes()
            # Match the store's universal-newline text reader while hashing the
            # actual bytes (also for hand-written CRLF files preserved by ensure).
            persisted = persisted_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            if persisted != expected:
                raise KnowledgeError("Persistenter Inhalt weicht vom erwarteten Ergebnis ab")
        except (KnowledgeError, OSError, UnicodeError) as error:
            self._failure(
                binding,
                payload,
                context,
                code="topic_knowledge_verification_failed",
                outcome="unknown",
            )
            raise TopicKnowledgeAmbiguousError(
                f"Dateiergebnis ist unbestaetigt. {_NO_AUTOMATIC_RETRY}"
            ) from error
        return hashlib.sha256(persisted_bytes).hexdigest()

    def _complete(self, event_type, binding, payload, context, **result):
        try:
            self._record(
                event_type,
                binding,
                {
                    **payload,
                    **result,
                    "completed_at": datetime.now(UTC).isoformat(),
                },
                context,
            )
        except Exception as error:
            raise TopicKnowledgeAmbiguousError(
                f"Dateiergebnis ist vorhanden, Audit-Zustand ungeklaert: Intent ohne Abschluss. {_NO_AUTOMATIC_RETRY}"
            ) from error

    @_application_operation
    def ensure_topic_knowledge(
        self, topic_id: str, *, context: EventContext | None = None
    ) -> TopicKnowledgeEnsureResult:
        """Explicit, repairable ensure; existing topic/legacy files are preserved."""
        resolved = self._load_binding(topic_id)
        binding = resolved.binding
        payload = self._intent(
            binding,
            resolved.knowledge_path,
            "ensure_topic_knowledge",
            context,
            TOPIC_KNOWLEDGE_INIT_INTENT_EVENT,
        )
        try:
            core = ContextKnowledgeService(self._session, self._store).ensure_context_knowledge(
                binding.context_id, context=context
            )
        except ContextKnowledgeError as error:
            self._failure(
                binding,
                payload,
                context,
                code="context_knowledge_initialization_failed",
                outcome="partial_structure_possible",
            )
            raise TopicKnowledgeError(
                "Context-Knowledge-Initialisierung ist fehlgeschlagen; Audit pruefen"
            ) from error
        knowledge_path = core.knowledge_path
        payload = {**payload, "knowledge_path": knowledge_path}
        # Uniform rule, and the order is load-bearing: ``_CONTENT_DEFECTS`` contains
        # ``KnowledgeContentError``, which *is* a ``KnowledgeError``. Listing the
        # generic tuple first would swallow it and make the content classification
        # dead code -- the whole point of the split.
        try:
            path = self._resolve_file(knowledge_path, binding.knowledge_key)
            existing = self._store._read_guarded(path)
        except _CONTENT_DEFECTS as error:
            raise self._content_defect(binding, payload, context, error) from error
        except (KnowledgeError, OSError) as error:
            self._failure(
                binding,
                payload,
                context,
                code="topic_knowledge_initialization_failed",
                outcome="not_written",
            )
            raise TopicKnowledgeError(
                f"Topic-Datei konnte nicht gelesen werden. {_NO_AUTOMATIC_RETRY}"
            ) from error
        try:
            content = existing if existing is not None else placeholder_document(binding.title)
            write = self._store._write(path, content)
        except _CONTENT_DEFECTS as error:
            # ``_write`` reads before it replaces, so a decode failure here still
            # precedes every filesystem mutation of ours.
            raise self._content_defect(binding, payload, context, error) from error
        except (KnowledgeError, OSError) as error:
            not_written = isinstance(error, _PRE_WRITE_ERRORS)
            self._failure(
                binding,
                payload,
                context,
                code="topic_knowledge_initialization_failed",
                outcome="not_written" if not_written else "unknown",
            )
            error_type = TopicKnowledgeError if not_written else TopicKnowledgeAmbiguousError
            raise error_type(
                f"Topic-Datei konnte nicht bestaetigt werden. {_NO_AUTOMATIC_RETRY}"
            ) from error
        content_hash = self._verify(path, write.content, binding, payload, context)
        self._complete(
            TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT
            if write.created
            else TOPIC_KNOWLEDGE_INIT_NOOP_EVENT,
            binding,
            payload,
            context,
            created=write.created,
            content_hash=content_hash,
        )
        return TopicKnowledgeEnsureResult(
            binding.topic_id,
            binding.context_id,
            binding.knowledge_key,
            knowledge_path,
            payload["relative_file"],
            payload["operation_id"],
            write.created,
            core.bound_now,
        )

    @_application_operation
    def apply_topic_update(
        self,
        topic_id: str,
        *,
        text: str | None = None,
        percent: int | None = None,
        context: EventContext | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> TopicKnowledgeWrite:
        """Append notes and/or replace qualitative progress; never changes DB status.

        An immediate identical block/percent is a no-op. This is not durable
        deduplication across intervening edits; ambiguous writes need human review.
        """
        if text is None and percent is None:
            raise TopicKnowledgeValidationError("Mindestens text oder percent ist erforderlich")
        if percent is not None and (type(percent) is not int or not 0 <= percent <= 100):
            raise TopicKnowledgeValidationError(
                "percent muss eine ganze Zahl zwischen 0 und 100 sein"
            )
        try:
            if text is not None and (not isinstance(text, str) or len(text) > MAX_TEXT_LENGTH):
                raise KnowledgeContentError("text muss ein String mit maximal 20000 Zeichen sein")
            body = _sanitize_text(text) if text is not None else None
            if body is not None:
                if not body.strip():
                    raise KnowledgeContentError("text darf nicht leer sein")
                body = body.strip()
                _reject_ambiguous_structure(body)
        except KnowledgeContentError as error:
            raise TopicKnowledgeValidationError("Ungueltiger Topic-Knowledge-Text") from error

        resolved = self._load_binding(topic_id)
        binding = resolved.binding
        try:
            knowledge_path = self._assert_context_ready(resolved.knowledge_path)
            path = self._resolve_file(knowledge_path, binding.knowledge_key)
            existing = self._store._read_guarded(path)
        except _CONTENT_DEFECTS as error:
            # No intent has been recorded yet and nothing has been written: a
            # hand-edited file that cannot even be decoded is not this caller's
            # doing. Report it without an intent -- there is no operation to audit.
            raise TopicKnowledgeContentError(
                "Die vorhandene Topic-Datei hat einen benutzerfixbaren Inhaltsfehler "
                f"({type(error).__name__}) und wird nicht automatisch ueberschrieben. {_NO_AUTOMATIC_RETRY}"
            ) from error
        except (KnowledgeError, OSError) as error:
            # Provably nothing written, but the read itself failed -- a permission
            # or filesystem problem that no retry resolves. The message carries the
            # no-retry contract explicitly, because this answer is a 503 and a 503
            # must never read as "the same call will succeed later".
            raise TopicKnowledgeError(
                f"Topic-Datei ist nicht sicher lesbar; es wurde nichts geschrieben. {_NO_AUTOMATIC_RETRY}"
            ) from error
        if existing is None:
            raise TopicKnowledgeNotInitializedError(
                "Topic-Knowledge muss zuerst explizit initialisiert werden"
            )

        payload = self._intent(
            binding,
            knowledge_path,
            "apply_topic_update",
            context,
            TOPIC_KNOWLEDGE_WRITE_INTENT_EVENT,
        )
        try:
            document = self._store._parse(existing).require_editable()
            notes = document.body_of(NOTES_SECTION).strip()
            progress = document.body_of(PROGRESS_SECTION)
            append = body is not None and not _ends_with_block(notes, body)
            progress_changed = (
                percent is not None and f"- Fortschritt: {percent} %" not in progress.splitlines()
            )
            if not append and not progress_changed:
                content = existing
            else:
                if progress_changed:
                    stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
                    document = document.with_body(
                        PROGRESS_SECTION, f"- Fortschritt: {percent} %\n- Aktualisiert: {stamp}"
                    )
                if append:
                    document = document.with_body(NOTES_SECTION, body, append=True)
                content = document.render()
            if before_write is not None:
                try:
                    before_write()
                except ConflictError:
                    self._failure(
                        binding, payload, context, code="precondition_failed", outcome="not_written"
                    )
                    raise
            write = self._store._write(path, content)
        except _CONTENT_DEFECTS as error:
            # All of these come from reading or structuring the existing file and
            # all of them happen before ``_store._write`` replaces anything.
            raise self._content_defect(binding, payload, context, error) from error
        except (KnowledgeError, OSError) as error:
            not_written = isinstance(error, _PRE_WRITE_ERRORS)
            self._failure(
                binding,
                payload,
                context,
                code="topic_knowledge_write_failed",
                outcome="not_written" if not_written else "unknown",
            )
            error_type = TopicKnowledgeError if not_written else TopicKnowledgeAmbiguousError
            raise error_type(
                f"Topic-Update konnte nicht bestaetigt werden. {_NO_AUTOMATIC_RETRY}"
            ) from error
        content_hash = self._verify(path, write.content, binding, payload, context)
        self._complete(
            TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT
            if write.changed
            else TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT,
            binding,
            payload,
            context,
            changed=write.changed,
            percent=percent,
            content_hash=content_hash,
        )
        return TopicKnowledgeWrite(
            binding.topic_id,
            binding.context_id,
            binding.knowledge_key,
            knowledge_path,
            payload["relative_file"],
            payload["operation_id"],
            write.changed,
            content_hash,
        )
