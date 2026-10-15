"""Whitelisted fields and column projections accepted by manifest verification."""

from __future__ import annotations

from typing import Any
from organiser.db.models import (
    Context,
    ContextSource,
    Resource,
    ResourceSnapshot,
    ResourceTopic,
    Task,
    TaskResource,
    Topic,
)
from organiser.domain.knowledge_path import (
    MAX_KNOWLEDGE_PATH_LENGTH,
)


# ---------------------------------------------------------------------------
# Manifest verification (read-only, for the propose/apply component)
# ---------------------------------------------------------------------------


# Bound for the *display* rendering of any change value.
_MAX_CHANGE_TEXT = 300


# Bound for the machine ``reference``. A reference the module itself produces is an
# entity id, a snapshot id or a store-relative knowledge address, and the store bounds a
# knowledge path to ``MAX_KNOWLEDGE_PATH_LENGTH`` characters (512); the longest store
# file name is appended to it. So every reference this module can emit is safe by
# construction, and the bound below refuses only what a forged manifest could not have
# gotten from this module at all.
_MAX_REFERENCE_CHARS = MAX_KNOWLEDGE_PATH_LENGTH + len("progress.md")


# Fixed replacements for a reference that is not a safe machine reference. They are
# deliberately bracket-free: they must stay readable as machine data, and they name the
# refusal instead of repeating what was refused.
_REFERENCE_PATH_MARKER = "absolute Pfad ausgelassen"


_REFERENCE_UNSAFE_MARKER = "unsichere Referenz zurueckgewiesen"


_MANIFEST_MODELS = {
    "context": Context,
    "task": Task,
    "topic": Topic,
    "resource": Resource,
    "context_source": ContextSource,
}


# The exact set of ``selected_fields`` keys each entity type may carry.
#
# This is a security boundary, not a convenience. ``EntityManifestEntry.selected_fields``
# is an open ``dict``, and the verifier compares every key it is given by reading the
# named attribute off the ORM row. Without a whitelist, a hand-crafted manifest could
# name ``locator`` or ``source_path_snapshot`` and have the verifier echo an absolute
# host path back in its change list -- the one thing the packet and the manifest both
# promise never to expose. An unknown key is therefore reported as *unverifiable*
# rather than silently ignored, so a manifest cannot pass verification by carrying
# fields nobody checks.
_MANIFEST_FIELDS: dict[str, frozenset[str]] = {
    "context": frozenset(
        {"title", "type", "status", "description", "parent_context_id", "knowledge_path"}
    ),
    "task": frozenset(
        {
            "title",
            "status",
            "topic_id",
            "parent_task_id",
            "project_id",
            "description",
            "due_at",
            "earliest_start_at",
            "estimated_minutes",
            "created_at",
            "resource_ids",
        }
    ),
    "topic": frozenset(
        {"title", "title_key", "status", "knowledge_key", "created_at", "resource_ids"}
    ),
    "resource": frozenset(
        {
            "title",
            "status",
            "relative_path",
            "resource_type",
            "current_content_hash",
            "byte_size",
            "snapshot_truncated",
            "created_at",
            "last_seen_at",
            "source_id",
            "topic_ids",
            "task_ids",
        }
    ),
    "context_source": frozenset({"source_type", "status", "label"}),
    # ``payload_read_cap`` is whitelisted because the verifier *reads* it (see
    # ``_event_payload_bound``) and refuses it as ``unverifiable`` if it were not. It is
    # the one event field that is an input rather than a compared value.
    "event": frozenset(
        {
            "event_type",
            "occurred_at",
            "entity_type",
            "entity_id",
            "payload_hash",
            "payload_length",
            "payload_read_cap",
        }
    ),
}


# Column subset the verifier loads. Anything outside it is never brought into memory,
# so an absolute source path cannot be materialised even by accident.
_MANIFEST_COLUMNS: dict[str, tuple[Any, ...]] = {
    "context": (
        Context.id,
        Context.title,
        Context.type,
        Context.status,
        Context.description,
        Context.parent_context_id,
        Context.knowledge_path,
        Context.updated_at,
    ),
    "task": (
        Task.id,
        Task.title,
        Task.status,
        Task.topic_id,
        Task.parent_task_id,
        Task.project_id,
        Task.description,
        Task.due_at,
        Task.earliest_start_at,
        Task.estimated_minutes,
        Task.created_at,
        Task.updated_at,
    ),
    "topic": (
        Topic.id,
        Topic.title,
        Topic.title_key,
        Topic.status,
        Topic.knowledge_key,
        Topic.created_at,
        Topic.updated_at,
    ),
    "resource": (
        Resource.id,
        Resource.title,
        Resource.status,
        Resource.relative_path,
        Resource.resource_type,
        Resource.current_content_hash,
        Resource.byte_size,
        Resource.created_at,
        Resource.last_seen_at,
        Resource.source_id,
        Resource.updated_at,
    ),
    "context_source": (
        ContextSource.id,
        ContextSource.source_type,
        ContextSource.status,
        ContextSource.path,
        ContextSource.updated_at,
    ),
}


_SNAPSHOT_MANIFEST_COLUMNS = (
    ResourceSnapshot.id,
    ResourceSnapshot.content_hash,
    ResourceSnapshot.extractor_version,
    ResourceSnapshot.status,
)


# The whitelist fields that are plain columns, so ``_actual_field`` can read them off the
# row. Events have no link tables, so the link and derived branches of ``_actual_field``
# never apply to them.
_EVENT_COLUMN_FIELDS = frozenset({"event_type", "occurred_at", "entity_type", "entity_id"})


# Link lists are not columns, so the verifier has to read the link tables. Each is
# read with the same per-item cap the builder used, because the manifest records what
# the packet actually showed. A link beyond that cap does not change the packet, and
# any audited change still moves the owning row's ``updated_at``.
_LINK_TABLES = {
    ("task", "resource_ids"): (TaskResource.task_id, TaskResource.resource_id),
    ("topic", "resource_ids"): (ResourceTopic.topic_id, ResourceTopic.resource_id),
    ("resource", "topic_ids"): (ResourceTopic.resource_id, ResourceTopic.topic_id),
    ("resource", "task_ids"): (TaskResource.resource_id, TaskResource.task_id),
}


# The only fields the builder truncated when it recorded them. Truncating a field it
# recorded verbatim -- ``entity_id``, a SHA-256, a status -- would compare a bounded
# value against a full one and report a change for untouched data.
_BOUND_TEXT_FIELDS = {
    "title": "max_title_chars",
    "type": "max_title_chars",
    "title_key": "max_title_chars",
    "source_type": "max_title_chars",
    "event_type": "max_event_type_chars",
    "entity_type": "max_event_entity_chars",
    "entity_id": "max_event_entity_chars",
    "description": "max_description_chars",
    "relative_path": "max_label_chars",
}
