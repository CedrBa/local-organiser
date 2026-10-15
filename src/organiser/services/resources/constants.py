"""Stable scan events, states, reasons and integrity classification constants."""

from __future__ import annotations

import logging
from organiser.domain.resources import (
    SnapshotStatus,
)


RUN_TYPE_SCAN = "resource_scan"


RUN_TYPE_EXTRACT = "resource_extraction"


STATUS_ACTIVE = "active"


STATUS_MISSING = "missing"


STATUS_ARCHIVED = "archived"


SCANNABLE_SOURCE_TYPES = ("folder", "document", "note")


EVENT_RESOURCE_REGISTERED = "RESOURCE_REGISTERED"


EVENT_RESOURCE_REFRESHED = "RESOURCE_REFRESHED"


EVENT_RESOURCE_MISSING = "RESOURCE_MISSING"


EVENT_RESOURCE_EXTRACTED = "RESOURCE_EXTRACTED"


EVENT_SOURCE_SCANNED = "CONTEXT_SOURCE_SCANNED"


EVENT_SOURCE_REJECTED = "CONTEXT_SOURCE_REJECTED"


REASON_SOURCE_REMOVED = "source_removed"


REASON_SOURCE_STATUS = "source_not_active"


REASON_RESOURCE_STATUS_MISSING = "resource_missing"


REASON_RESOURCE_STATUS_ARCHIVED = "resource_archived"


REASON_RESOURCE_HASH_MOVED = "resource_hash_moved"


REASON_STALE_MISSING = "resource_is_missing"


REASON_STALE_ARCHIVED = "resource_is_archived"


REASON_STALE_NO_SNAPSHOT = "no_snapshot_for_current_hash"


#: A raw OSError from the reader (EIO on a failing disk, ESTALE on NFS) is a
#: normal outcome of a freshness check, not a crash of the service.
REASON_FILE_UNREADABLE = "file_unreadable"


#: A zero-length original: it can never carry evidence, so it never passes.
REASON_RESOURCE_EMPTY = "empty_file"


# A snapshot counts as *current* only if it is ready and its content hash equals
# the resource's current content hash.
CURRENT_SNAPSHOT_STATUSES = (SnapshotStatus.READY.value,)


_LOCATOR_QUERY_CHUNK = 400


# -- scan --------------------------------------------------------------------


_SCAN_LOG = logging.getLogger(__name__)


_REGISTRY_UNIQUE_MESSAGES = frozenset(
    {
        "UNIQUE constraint failed: resources.context_id, resources.locator",
        "UNIQUE constraint failed: resource_snapshots.resource_id, resource_snapshots.content_hash, resource_snapshots.extractor_version",
    }
)
