"""Explicit DB-key-addressed topic knowledge, with owned session and file writes.

DB title/status remain canonical. Markdown stores qualitative notes/progress only.
The shared store's process-local lock covers read/modify/write and verification;
there is no DB/filesystem atomicity, multi-process lock or automatic retry.

Public imports remain available here; implementation lives in focused modules.
"""

from .contracts import (
    TOPIC_KNOWLEDGE_INIT_INTENT_EVENT as TOPIC_KNOWLEDGE_INIT_INTENT_EVENT,
    TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT as TOPIC_KNOWLEDGE_INIT_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_INIT_NOOP_EVENT as TOPIC_KNOWLEDGE_INIT_NOOP_EVENT,
    TOPIC_KNOWLEDGE_WRITE_INTENT_EVENT as TOPIC_KNOWLEDGE_WRITE_INTENT_EVENT,
    TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT as TOPIC_KNOWLEDGE_WRITE_COMPLETED_EVENT,
    TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT as TOPIC_KNOWLEDGE_WRITE_NOOP_EVENT,
    TOPIC_KNOWLEDGE_FAILED_EVENT as TOPIC_KNOWLEDGE_FAILED_EVENT,
    PROGRESS_SECTION as PROGRESS_SECTION,
    NOTES_SECTION as NOTES_SECTION,
    MAX_TEXT_LENGTH as MAX_TEXT_LENGTH,
    _PRE_WRITE_ERRORS as _PRE_WRITE_ERRORS,
    _NO_AUTOMATIC_RETRY as _NO_AUTOMATIC_RETRY,
    TopicKnowledgeError as TopicKnowledgeError,
    TopicKnowledgeValidationError as TopicKnowledgeValidationError,
    TopicKnowledgeNotInitializedError as TopicKnowledgeNotInitializedError,
    TopicKnowledgeAmbiguousError as TopicKnowledgeAmbiguousError,
    TopicKnowledgeContentError as TopicKnowledgeContentError,
    _CONTENT_DEFECTS as _CONTENT_DEFECTS,
    _ResolvedTopic as _ResolvedTopic,
    TopicKnowledgeEnsureResult as TopicKnowledgeEnsureResult,
    TopicKnowledgeWrite as TopicKnowledgeWrite,
)
from .document import (
    placeholder_document as placeholder_document,
)
from .boundary import (
    _application_operation as _application_operation,
)
from .service import (
    TopicKnowledgeService as TopicKnowledgeService,
)
