"""Topic-knowledge outcomes, stable audit vocabulary and controlled errors."""

from __future__ import annotations

from dataclasses import dataclass
from organiser.domain.topics import TopicBinding
from organiser.services.context_knowledge import (
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
)
from organiser.services.domain import (
    DomainValidationError,
)
from organiser.services.knowledge import (
    KnowledgeContentError,
    UnsafeKnowledgePathError,
)


TOPIC_KNOWLEDGE_INIT_INTENT_EVENT = "TOPIC_KNOWLEDGE_INIT_INTENT"


TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT = "TOPIC_KNOWLEDGE_INIT_COMPLETED"


TOPIC_KNOWLEDGE_INIT_NOOP_EVENT = "TOPIC_KNOWLEDGE_INIT_NOOP"


TOPIC_KNOWLEDGE_WRITE_INTENT_EVENT = "TOPIC_KNOWLEDGE_WRITE_INTENT"


TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT = "TOPIC_KNOWLEDGE_WRITE_COMPLETED"


TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT = "TOPIC_KNOWLEDGE_WRITE_NOOP"


TOPIC_KNOWLEDGE_FAILED_EVENT = "TOPIC_KNOWLEDGE_FAILED"


PROGRESS_SECTION = "Lernstand"


NOTES_SECTION = "Notizen"


MAX_TEXT_LENGTH = 20_000


_PRE_WRITE_ERRORS = (KnowledgeContentError, UnsafeKnowledgePathError)


_NO_AUTOMATIC_RETRY = (
    "Der Vorgang wird nicht automatisch wiederholt; bitte Datei und Audit pruefen."
)


class TopicKnowledgeError(ContextKnowledgeError):
    code = "topic_knowledge_failed"


class TopicKnowledgeValidationError(TopicKnowledgeError, DomainValidationError):
    code = "invalid_topic_knowledge_update"


class TopicKnowledgeNotInitializedError(TopicKnowledgeError, ContextKnowledgeNotInitializedError):
    code = "topic_knowledge_not_initialized"


class TopicKnowledgeAmbiguousError(TopicKnowledgeError, ContextKnowledgeAmbiguousError):
    code = "topic_knowledge_ambiguous"


class TopicKnowledgeContentError(TopicKnowledgeError):
    """A user-fixable defect *in the stored markdown file*, not in the request.

    This is the one classification that must never look transient. Examples: two
    ``## Lernstand`` sections in one topic file, a managed section that appears
    twice in a case-folded spelling, too many sections, or a topic file that is
    not valid UTF-8 at all.

    Every one of these is decided by *reading the existing file*, before this
    service writes anything, so the outcome is provably ``not_written`` and there
    is nothing to retry: an identical call fails identically until a human edits
    the file. Reporting them as a generic :class:`TopicKnowledgeError` mapped the
    HTTP answer to 503, and a 503 is precisely the signal a client (or E's
    recovery logic) reads as "the same request may succeed later". It cannot. The
    difference is a file the user has to open.

    It is deliberately **not** a :class:`DomainValidationError`: that class is
    caught first at the API boundary and means "this request is malformed", which
    would send the caller looking at the wrong thing.
    """

    code = "topic_knowledge_content_invalid"


#: Non-transient, user-actionable file content defects. A ``UnicodeError`` counts
#: because a byte sequence that is not UTF-8 can never be parsed or rewritten
#: correctly by a retry; only editing the file helps.
#:
#: This tuple must always be caught **before** ``(KnowledgeError, OSError)``.
#: ``KnowledgeContentError`` subclasses ``KnowledgeError``, so the reverse order
#: silently routes every content defect into the generic -- and transient-looking --
#: branch, which is precisely the classification this class exists to prevent.
_CONTENT_DEFECTS: tuple[type[BaseException], ...] = (KnowledgeContentError, UnicodeError)


@dataclass(frozen=True)
class _ResolvedTopic:
    binding: TopicBinding
    knowledge_path: str | None


@dataclass(frozen=True)
class TopicKnowledgeEnsureResult:
    topic_id: str
    context_id: str
    knowledge_key: str
    knowledge_path: str
    relative_file: str
    operation_id: str
    created: bool
    context_bound_now: bool


@dataclass(frozen=True)
class TopicKnowledgeWrite:
    topic_id: str
    context_id: str
    knowledge_key: str
    knowledge_path: str
    relative_file: str
    operation_id: str
    changed: bool
    content_hash: str
