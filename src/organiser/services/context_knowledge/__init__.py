"""Controlled connection between persisted Contexts and the markdown knowledge store.

The database is canonical for context ID, lifecycle, hierarchy, sources and the
knowledge path binding. Markdown is canonical for the qualitative knowledge
content; percentages live in markdown only.

This service is the narrow seam between the two. It never invents a path: it loads
the context, checks or binds its ``knowledge_path``, and only then asks the
``KnowledgeStore`` to write. The store stays an internal filesystem adapter and is
not reachable through the context-addressed DTO.

Transaction boundary
--------------------

Every public method is one explicit application transaction and it *owns* it:

* Entry is refused while the caller's session is doing anything at all -- an open
  transaction (``in_transaction()``, which also covers already flushed changes and
  raw ``session.execute`` writes), a ``defer_domain_commit`` marker, or pending
  ORM state (``new``/``dirty``/``deleted``). ``new``/``dirty``/``deleted`` alone
  cannot see a flush, and a flush alone is enough for ``commit()`` to carry
  foreign work, so both are required.
* The caller ends its own transaction before calling. Foreign work is never
  committed and never rolled back by this service: there is no hidden commit or
  rollback to manufacture a clean session.
* The service closes its own transaction cleanly: every failure path rolls back,
  so the caller is never left inside a half-open transaction of ours.

Fresh, immutable binding
------------------------

The binding is a one-time replacement of ``NULL`` by a value:

* The path is read as a column query, never through the ORM identity map, so a
  long-lived session cannot resurrect a stale ``NULL``.
* The bind is a conditional ``UPDATE ... WHERE knowledge_path IS NULL``. A
  binding that already exists is never overwritten, by this service and not by a
  context PATCH.
* Default, explicit binding and concurrent binding therefore converge on exactly
  one permanent value.

Binding is not initialisation: ``apply`` requires the complete regular core
structure (``overview.md``, ``progress.md``, ``notes.md``, ``topics/``) and
rejects a missing or partial one without creating anything. Only
``ensure_context_knowledge`` creates, and it stays explicit and repeatable.

Audit
-----

Every operation gets its own ``operation_id``. Intent, completion, no-op and
failure payloads carry it, so they belong together even without an
``EventContext`` and even for repeated identical calls. Intent is always written
before the filesystem mutation, the completion only after the persisted result
has been read back. A write whose outcome cannot be proven is never reported as a
success and never retried automatically.

Single writer: all writes for the integration path go through one shared
``KnowledgeStore`` instance (Single-Writer/Single-Process MVP). The store
serialises its own operations with a process-local lock. There is no cross-process
locking and no retry infrastructure. See
``docs/architecture/0009-context-knowledge-integration.md``.

Public imports remain available here; implementation lives in focused modules.
"""

from .contracts import (
    CORE_FILES as CORE_FILES,
    TOPICS_DIRECTORY as TOPICS_DIRECTORY,
    CORE_STRUCTURE as CORE_STRUCTURE,
    KNOWLEDGE_PATH_BOUND_EVENT as KNOWLEDGE_PATH_BOUND_EVENT,
    KNOWLEDGE_INIT_INTENT_EVENT as KNOWLEDGE_INIT_INTENT_EVENT,
    KNOWLEDGE_INIT_COMPLETED_EVENT as KNOWLEDGE_INIT_COMPLETED_EVENT,
    KNOWLEDGE_INIT_NOOP_EVENT as KNOWLEDGE_INIT_NOOP_EVENT,
    KNOWLEDGE_INIT_FAILED_EVENT as KNOWLEDGE_INIT_FAILED_EVENT,
    KNOWLEDGE_WRITE_INTENT_EVENT as KNOWLEDGE_WRITE_INTENT_EVENT,
    KNOWLEDGE_WRITE_COMPLETED_EVENT as KNOWLEDGE_WRITE_COMPLETED_EVENT,
    KNOWLEDGE_WRITE_NOOP_EVENT as KNOWLEDGE_WRITE_NOOP_EVENT,
    KNOWLEDGE_WRITE_FAILED_EVENT as KNOWLEDGE_WRITE_FAILED_EVENT,
    _KNOWLEDGE_EVENTS as _KNOWLEDGE_EVENTS,
    _PRE_WRITE_ERRORS as _PRE_WRITE_ERRORS,
    OUTCOME_NOT_WRITTEN as OUTCOME_NOT_WRITTEN,
    OUTCOME_UNKNOWN as OUTCOME_UNKNOWN,
    OUTCOME_PARTIAL_STRUCTURE as OUTCOME_PARTIAL_STRUCTURE,
    _NO_AUTOMATIC_RETRY as _NO_AUTOMATIC_RETRY,
    ContextKnowledgeError as ContextKnowledgeError,
    ContextKnowledgeNotInitializedError as ContextKnowledgeNotInitializedError,
    ContextKnowledgeAmbiguousError as ContextKnowledgeAmbiguousError,
    _VerificationFailed as _VerificationFailed,
    ContextKnowledgeResult as ContextKnowledgeResult,
    ContextKnowledgeWrite as ContextKnowledgeWrite,
    _ContextBinding as _ContextBinding,
)
from .values import (
    _digest as _digest,
    _timestamp as _timestamp,
    _operation_id as _operation_id,
    _relative_file as _relative_file,
)
from .boundary import (
    KnowledgeBoundary as KnowledgeBoundary,
)
from .audit import (
    KnowledgeAudit as KnowledgeAudit,
)
from .binding import (
    KnowledgeBinding as KnowledgeBinding,
)
from .verification import (
    KnowledgeVerification as KnowledgeVerification,
)
from .service import (
    ContextKnowledgeService as ContextKnowledgeService,
)
from .shared_store import (
    _SHARED_STORE_LOCK as _SHARED_STORE_LOCK,
    _SHARED_STORES as _SHARED_STORES,
    shared_knowledge_store as shared_knowledge_store,
)
