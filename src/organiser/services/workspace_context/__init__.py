"""Bounded, read-only Context Workspace read model.

This package is the only place that assembles :class:`ContextWorkspacePacket`. It
answers one question -- "what is the current, evidence-backed state of exactly
this context?" -- and it answers it without writing anything.

Hard boundaries, all of them observable in the tests
----------------------------------------------------

* **One context, asked for.** The packet is built for a single explicit
  ``context_id``. Child contexts, sibling life areas and projects are never
  aggregated, and a project is never used to guess a context. A project only
  shows up as the ``project_id`` of a task that is itself bound to this context.
* **Deterministic selection.** Every list has a fixed order and every decision is
  derived from the database, never from a model. Explicitly requested topic and
  resource IDs are honoured, but they must belong to the requested context; a
  foreign or unknown ID is refused instead of silently resolved somewhere else.
* **No source file is ever opened.** The builder reads ``resource_snapshots.text``
  -- a bounded, derived extraction cache written by the ingestion component -- and
  nothing else about a source. ``locator``, ``source_path_snapshot`` and
  ``ContextSource.path`` are not part of the packet, and an absolute host address in an
  event payload is withheld in values *and* keys. The one path field that is copied
  verbatim is ``context.knowledge_path``; see "The one verbatim field" below.
* **No stale text is presented as current.** An excerpt is only emitted when the
  resource is active, its source is active, its ``current_content_hash`` is known
  and a ``ready`` snapshot exists for exactly that hash. Otherwise the resource
  carries an explicit ``knowledge_status`` and a warning. Older extract text is
  never silently reused.
* **Every field is bounded, and the whole packet is bounded.** Counts, per-field
  text limits, the four shared content excerpts at 4.000 characters each, and a
  final check on the real serialized JSON length. Reductions are deterministic,
  counted in ``selection.omitted_counts`` and announced in ``warnings``. One field
  is bounded by a *different* ceiling -- the write-side contract -- and says so
  rather than pretending the policy bounds it.
* **No writes and no learning.** The builder never writes knowledge or the
  database, never creates the knowledge root, and never persists anything it
  inferred. It is a pure read model.
* **Untrusted text stays data.** Every excerpt carries
  ``trust="untrusted_evidence"``, the packet carries
  ``evidence_policy="untrusted_data_not_instructions"``, and event payloads are
  bounded projections rather than raw JSON. IDs, hashes, versions and timestamps
  come from database columns, so a document can never redefine its own identity.
* **The manifest is the only freshness signal.** ``freshness.authoritative`` is
  ``False`` by construction: knowledge files and source documents change without
  producing an event, so a global event id proves nothing. The manifest records
  every used entity with its status and ``updated_at``, every used snapshot with
  its original hash and extractor version, and the real content hash of every
  knowledge file that was read. :func:`verify_workspace_manifest` re-checks all of
  it read-only for the propose/apply component.

The non-leaking knowledge_path representation
----------------------------------------------

``ContextSummary.knowledge_path`` and the manifest's ``selected_fields
["knowledge_path"]`` carry valid stored values **unchanged** (relative, at most
``MAX_KNOWLEDGE_PATH_LENGTH`` (512) characters). A row that violates the write contract --
written by raw SQL or an external tool that bypassed domain validation, such as an
absolute host path or traversal -- is never exposed raw in the packet or manifest.
Instead, it is projected as a deterministic, non-leaking digest marker
(``<invalid:sha256:...>``).

This ensures that:
1. No host paths, directory names or raw invalid paths ever leak into the serialized
   packet or manifest.
2. Freshness remains strictly verifiable in :func:`verify_workspace_manifest`: the
   digest changes whenever the database row changes, and compares equal when untouched.
3. Every knowledge read for such a context is cleanly refused by the store, producing no
   knowledge excerpts and a visible warning.

Determinism
-----------

The packet contains no generation timestamp, no elapsed time and no ordering that
depends on Python dict or filesystem iteration order. Two calls against unchanged
data therefore produce byte-identical JSON, which is what makes the manifest
meaningful as an apply precondition.

Public imports remain available here; implementation lives in focused modules.
"""

from .policy import (
    WorkspacePolicy as WorkspacePolicy,
    DEFAULT_WORKSPACE_POLICY as DEFAULT_WORKSPACE_POLICY,
    WorkspaceSelectionError as WorkspaceSelectionError,
    WorkspaceBudgetError as WorkspaceBudgetError,
)
from .constants import (
    _TRUNCATION_MARKER as _TRUNCATION_MARKER,
    _ABSOLUTE_PATH_MARKER as _ABSOLUTE_PATH_MARKER,
    _WINDOWS_ABSOLUTE_PATH as _WINDOWS_ABSOLUTE_PATH,
    _PATH_PREFIX_PATTERN as _PATH_PREFIX_PATTERN,
    _CANONICAL_TOPIC_STEM as _CANONICAL_TOPIC_STEM,
    OPEN_TASK_STATUSES as OPEN_TASK_STATUSES,
    CORE_KNOWLEDGE_FILES as CORE_KNOWLEDGE_FILES,
    _SNAPSHOT_META_COLUMNS as _SNAPSHOT_META_COLUMNS,
)
from .redaction import (
    _truncate as _truncate,
    _looks_like_absolute_path as _looks_like_absolute_path,
    _absolute_path_spans as _absolute_path_spans,
    _contains_absolute_path as _contains_absolute_path,
    _redact_absolute_paths as _redact_absolute_paths,
    _INVALID_KNOWLEDGE_PATH_MARKER as _INVALID_KNOWLEDGE_PATH_MARKER,
    _safe_knowledge_path as _safe_knowledge_path,
    _is_safe_knowledge_path as _is_safe_knowledge_path,
    _bound_payload_key as _bound_payload_key,
    _bound_payload as _bound_payload,
    _page_span as _page_span,
    _source_label as _source_label,
)
from .values import (
    _stamp as _stamp,
    _stamp_key as _stamp_key,
    _instant_key as _instant_key,
    _digest_json as _digest_json,
    _first_int as _first_int,
)
from .budget import (
    _Budget as _Budget,
    _bounded_warnings as _bounded_warnings,
    _select_excerpts as _select_excerpts,
)
from .state import (
    _ExcerptCandidate as _ExcerptCandidate,
    _EventRow as _EventRow,
    _ExcerptSelection as _ExcerptSelection,
    _Prepared as _Prepared,
)
from .builder import (
    workspace_context as workspace_context,
    workspace_excerpt_summary as workspace_excerpt_summary,
    _assemble as _assemble,
    _assemble_once as _assemble_once,
)
from .selection import (
    _load_context as _load_context,
    _validate_focus as _validate_focus,
    _prepare as _prepare,
    _select_resources as _select_resources,
    _load_referenced_sources as _load_referenced_sources,
    _load_links as _load_links,
)
from .events import (
    _count as _count,
    _event_clauses as _event_clauses,
    _event_payload_expression as _event_payload_expression,
    _event_payload_columns as _event_payload_columns,
    _event_row_from_columns as _event_row_from_columns,
    _load_event_row as _load_event_row,
    _recent_events as _recent_events,
    _count_events as _count_events,
)
from .snapshots import (
    _load_snapshots as _load_snapshots,
    _classify_resources as _classify_resources,
    _load_excerpt_text as _load_excerpt_text,
)
from .knowledge import (
    _add_knowledge_candidates as _add_knowledge_candidates,
)
from .projection import (
    _to_excerpt as _to_excerpt,
    _packet_evidence as _packet_evidence,
    _task_summary as _task_summary,
    _topic_summary as _topic_summary,
    _resource_summary as _resource_summary,
    _source_summary as _source_summary,
    _event_summary as _event_summary,
)
from .manifest import (
    _build_manifest as _build_manifest,
)
from .verification_contracts import (
    _MAX_CHANGE_TEXT as _MAX_CHANGE_TEXT,
    _MAX_REFERENCE_CHARS as _MAX_REFERENCE_CHARS,
    _REFERENCE_PATH_MARKER as _REFERENCE_PATH_MARKER,
    _REFERENCE_UNSAFE_MARKER as _REFERENCE_UNSAFE_MARKER,
    _MANIFEST_MODELS as _MANIFEST_MODELS,
    _MANIFEST_FIELDS as _MANIFEST_FIELDS,
    _MANIFEST_COLUMNS as _MANIFEST_COLUMNS,
    _SNAPSHOT_MANIFEST_COLUMNS as _SNAPSHOT_MANIFEST_COLUMNS,
    _EVENT_COLUMN_FIELDS as _EVENT_COLUMN_FIELDS,
    _LINK_TABLES as _LINK_TABLES,
    _BOUND_TEXT_FIELDS as _BOUND_TEXT_FIELDS,
)
from .verification_results import (
    WorkspaceManifestChange as WorkspaceManifestChange,
    _reference_text as _reference_text,
    _change_text as _change_text,
    WorkspaceManifestVerification as WorkspaceManifestVerification,
)
from .verification import (
    verify_workspace_manifest as verify_workspace_manifest,
)
from .verification_knowledge import (
    _knowledge_address_reason as _knowledge_address_reason,
)
from .verification_entities import (
    _event_payload_bound as _event_payload_bound,
    _verify_event as _verify_event,
    _link_ids as _link_ids,
    _actual_field as _actual_field,
)
