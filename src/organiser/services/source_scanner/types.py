"""Observation results and charged read/entry budgets for bounded ingestion."""

from __future__ import annotations

import os
from dataclasses import dataclass


CHUNK_SIZE = 1024 * 1024


#: Whether ``/proc/self/fd`` exists on this host. Without procfs the containment
#: check cannot run; the descriptor walk is then the only guarantee, and the
#: module says so instead of pretending the check passed.
PROCFS_AVAILABLE = os.path.isdir("/proc/self/fd")


# Reasons a single observation carries instead of a content hash. They are stable
# codes because the API reports them and a client may branch on them.
REASON_FILE_TOO_LARGE = "file_too_large"


REASON_READ_BUDGET = "read_budget_exhausted"


REASON_UNREADABLE = "file_unreadable"


REASON_UNSTABLE = "file_unstable"


REASON_DIRECTORY = "not_a_regular_file"


#: A zero-length observation. It is a legitimate state of a file and it is *not*
#: reported as "the file changed while it was read", but it is also not a version:
#: see the empty-observation policy in :func:`_hash_open_file`.
REASON_EMPTY = "empty_file"


#: The descriptor ended before the byte count the file itself announced. Same
#: string as the typed refusal of :func:`read_local_file`, so one file with one
#: cause has one code no matter which of the two readers saw it first.
REASON_SHORT_READ = "file_short_read"


# Reasons an enumeration stopped before it saw the whole source scope. Any of
# these makes the scan incomplete, and an incomplete scan may never mark a
# resource missing.
STOP_ENTRY_LIMIT = "entry_limit_reached"


STOP_DEPTH_LIMIT = "depth_limit_reached"


STOP_READ_BUDGET = "read_budget_exhausted"


STOP_UNREADABLE_DIRECTORY = "directory_unreadable"


STOP_UNSAFE_DIRECTORY = "directory_not_entered"


#: Refusals that describe the *configuration* of a source rather than a temporary
#: condition. A temporary condition makes a scan incomplete; a permanent one is an
#: error the operator has to see.
PERMANENT_REFUSALS = frozenset(
    {"symlink", "special_file", "not_a_directory", "containment_failed", "containment_unavailable"}
)


@dataclass(frozen=True)
class ObservedFile:
    """One regular file seen during enumeration.

    ``content_hash`` is the SHA256 of the bytes that were actually read. It is
    ``None`` whenever the bytes were not read (too large, budget spent, unreadable
    or unstable); the resource then simply has no known current version.
    """

    relative_path: str
    locator: str
    title: str
    resource_type: str
    byte_size: int
    mtime_ns: int
    content_hash: str | None = None
    unread_reason: str | None = None


@dataclass
class ReadBudget:
    """Mutable read accounting for one bounded operation."""

    max_total_bytes: int
    used_bytes: int = 0

    @property
    def remaining(self) -> int:
        return max(0, self.max_total_bytes - self.used_bytes)

    @property
    def exhausted(self) -> bool:
        return self.used_bytes >= self.max_total_bytes

    def charge(self, count: int) -> None:
        self.used_bytes += count


@dataclass(frozen=True)
class SourceTreeResult:
    files: tuple[ObservedFile, ...] = ()
    complete: bool = True
    stop_reason: str | None = None
    skipped_unsafe: int = 0
    skipped_excluded: int = 0
    unreadable_directories: int = 0
    read_budget_used: int = 0
    #: Directory entries the walk charged to ``max_entries``. It counts every entry
    #: it looked at -- files, subdirectories, excluded names and unsafe objects --
    #: not only the regular files that became observations.
    entries_seen: int = 0

    @property
    def truncated(self) -> bool:
        return self.stop_reason is not None


@dataclass(frozen=True)
class FileRead:
    """Bytes plus the identity they were read under.

    ``is_empty`` marks the one observation that must not become a version: a file
    that is genuinely zero bytes long. ``content_hash`` is then the well-known hash
    of no bytes, and callers that persist a version have to keep their previous one.
    """

    data: bytes
    byte_size: int
    mtime_ns: int
    content_hash: str
    is_empty: bool = False
