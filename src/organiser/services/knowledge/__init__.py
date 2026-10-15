"""Filesystem knowledge store: the only component allowed to write Organiser markdown.

The store owns a configurable knowledge root. Every context is a directory below
that root and every write goes through a validated, path-safe helper. Models never
receive a real path; they only produce a ``KnowledgeUpdate``, which is applied here.

This is an internal adapter, not an authorization boundary: the store has no notion
of users, contexts or permissions. A caller must decide *whether* a context may be
written; the store only decides *how* the file is addressed and edited.

Public imports remain available here; implementation lives in focused modules.
"""

from .contracts import (
    OVERVIEW_FILE as OVERVIEW_FILE,
    PROGRESS_FILE as PROGRESS_FILE,
    NOTES_FILE as NOTES_FILE,
    TOPICS_DIR as TOPICS_DIR,
    CORE_FILES as CORE_FILES,
    DIRECTORY_MODE as DIRECTORY_MODE,
    FILE_MODE as FILE_MODE,
    MAX_PATH_LENGTH as MAX_PATH_LENGTH,
    MAX_TEXT_LENGTH as MAX_TEXT_LENGTH,
    MAX_SECTION_ENTRIES as MAX_SECTION_ENTRIES,
    MAX_TOPIC_NAME_LENGTH as MAX_TOPIC_NAME_LENGTH,
    MAX_EXCERPT_BYTES as MAX_EXCERPT_BYTES,
    MAX_TOPIC_FILE_SCAN as MAX_TOPIC_FILE_SCAN,
    _UNSAFE_SEGMENT as _UNSAFE_SEGMENT,
    _SECTION_HEADING as _SECTION_HEADING,
    _DOCUMENT_TITLE as _DOCUMENT_TITLE,
    _FENCE as _FENCE,
    _WINDOWS_DRIVE as _WINDOWS_DRIVE,
    CORE_HEADINGS as CORE_HEADINGS,
    KnowledgeError as KnowledgeError,
    UnsafeKnowledgePathError as UnsafeKnowledgePathError,
    KnowledgeContentError as KnowledgeContentError,
    KnowledgeNotFoundError as KnowledgeNotFoundError,
    KnowledgeExcerptTooLargeError as KnowledgeExcerptTooLargeError,
    KnowledgeUnstableError as KnowledgeUnstableError,
    KnowledgeExcerptRead as KnowledgeExcerptRead,
    KnowledgeContext as KnowledgeContext,
    KnowledgeWrite as KnowledgeWrite,
    _HISTORY_ENTRY as _HISTORY_ENTRY,
)
from .document import (
    _Document as _Document,
    _now as _now,
    _timestamp as _timestamp,
    _sanitize_text as _sanitize_text,
    _fence_scan as _fence_scan,
    _ends_with_block as _ends_with_block,
    _reject_ambiguous_structure as _reject_ambiguous_structure,
    _check_heading as _check_heading,
    _truncate_text as _truncate_text,
    MarkdownCodec as MarkdownCodec,
    parts_title as parts_title,
    _status_lines as _status_lines,
    _status_semantics as _status_semantics,
    _timestamped_status as _timestamped_status,
    _last_history_facts as _last_history_facts,
    _history_entry as _history_entry,
    _history_facts as _history_facts,
)
from .paths import (
    _slug as _slug,
)
from .filesystem import (
    KnowledgeFilesystem as KnowledgeFilesystem,
)
from .reader import (
    KnowledgeReader as KnowledgeReader,
)
from .updates import (
    KnowledgeUpdates as KnowledgeUpdates,
)
from .store import (
    KnowledgeStore as KnowledgeStore,
    get_knowledge_store as get_knowledge_store,
)
