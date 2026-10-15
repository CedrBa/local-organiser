"""Canonical selection vocabulary and metadata-only snapshot projections."""

from __future__ import annotations

import re
from organiser.db.models import (
    ResourceSnapshot,
)


# ---------------------------------------------------------------------------
# Constants and small pure helpers
# ---------------------------------------------------------------------------

_TRUNCATION_MARKER = " [...]"


# Replaces an absolute host address in an event payload -- in a value *and* in a key.
# The packet promises no absolute source paths -- not in the excerpts, not in the
# manifest -- and ``recent_history`` is read by the very prompts those excerpts feed.
# A legacy ``CONTEXT_SOURCE_ADDED`` row stores exactly the path its writer registered,
# so without this the guarantee would hold everywhere except in history. The payload
# shape stays: only the address itself is withheld, and the projection is marked
# truncated so the omission is visible rather than silent.
_ABSOLUTE_PATH_MARKER = "<absolute Pfad ausgelassen>"


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


_PATH_PREFIX_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"("
    r"file://[^\s\"\'\`<>|]+"
    r"|"
    r"\\\\[^/\\\\\"\'\`\s<>|]+[/\\\\]"
    r"|"
    r"[A-Za-z]:[/\\\\]"
    r"|"
    r"/(?![/\s])"
    r")"
)


# A canonical topic file stem is ``topic-<uuid>`` (the schema contract). Any
# other stem below ``topics/`` predates the knowledge_key binding and is treated as
# legacy knowledge -- never as a topic's file, and never as its status.
_CANONICAL_TOPIC_STEM = re.compile(
    r"^topic-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# A task is "current" while it still asks for something. Completing a task does not
# complete a topic, and neither direction is inferred here.
OPEN_TASK_STATUSES = ("open", "in_progress", "waiting")


CORE_KNOWLEDGE_FILES = ("overview.md", "progress.md", "notes.md")


# Snapshot columns read without their text. Keeping ``text`` out of the metadata
# scan is what stops one context with many snapshots from pulling hundreds of
# 200.000-character blobs into memory for a packet that shows four excerpts.
_SNAPSHOT_META_COLUMNS = (
    ResourceSnapshot.id,
    ResourceSnapshot.resource_id,
    ResourceSnapshot.content_hash,
    ResourceSnapshot.extractor_version,
    ResourceSnapshot.status,
    ResourceSnapshot.truncated,
    ResourceSnapshot.metadata_payload,
    ResourceSnapshot.error_code,
    ResourceSnapshot.created_at,
)
