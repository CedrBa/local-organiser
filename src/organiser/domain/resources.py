"""Pure rules and typed results of the resource registry and controlled intake.

This module is the single place that decides *what* a resource observation means.
It performs no database access, no filesystem access and no network access: the
filesystem rules live in :mod:`organiser.services.source_scanner`, the byte to
text rules in :mod:`organiser.services.extraction`, and the persistence in
:mod:`organiser.services.resources`.

Three rules are load bearing and are therefore expressed only here:

* A locator is a canonical, absolute, non-resolved path string. It is the
  internal identity of a resource inside one context. Symlinks are never
  resolved into it, because a resolved locator would grant a stable identity to
  a link target and silently merge two different originals.
* Observation completeness and extraction success are independent axes. A scan
  that hit a budget is not a scan that proved absence, and a failed extraction
  is not a missing file.
* The budgets are fixed constants, not settings and not request fields. They can
  be lowered by an embedding application, never raised.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Literal

from pydantic import Field

from organiser.domain.workspace_contracts import SnapshotStatus, WorkspaceDTO


# Source types whose *path* is a local filesystem entry this component may read.
# "manual", "link" and "inbox" carry no readable local address by contract.
SUPPORTED_SOURCE_TYPES = ("folder", "document", "note")
UNSUPPORTED_SOURCE_TYPES = ("manual", "link", "inbox")

RESOURCE_TYPES = ("pdf", "markdown", "text", "other")
RESOURCE_STATUSES = ("active", "missing", "archived")

TEXT_SUFFIXES = frozenset({".txt", ".text"})
MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})
PDF_SUFFIXES = frozenset({".pdf"})

MAX_TITLE_LENGTH = 200
MAX_RELATIVE_PATH_LENGTH = 1024

# Directory names that are never a source. Hidden entries are matched separately
# by their leading dot, so this set only carries named build/VCS/tooling folders.
EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        "node_modules",
        "venv",
        "site-packages",
        "dist-info",
    }
)
VCS_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn", ".bzr"})
VENV_DIRECTORY_NAMES = frozenset({".venv", "venv", "virtualenv", ".virtualenv"})

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


@dataclass(frozen=True)
class IngestionLimits:
    """Fixed upper bounds for one scan or one extraction.

    The defaults are the values fixed by the architecture. ``replace`` may only
    lower them; :func:`narrowed` enforces that, so a caller cannot raise a budget
    by accident and no request field can reach this object at all.
    """

    max_entries: int = 2_000
    max_depth: int = 10
    max_file_bytes: int = 20 * 1024 * 1024
    max_total_read_bytes: int = 100 * 1024 * 1024
    max_extractions: int = 200
    max_text_characters: int = 200_000
    max_pdf_pages: int = 200
    # Parser isolation, see services/extraction.py.
    pdf_worker_timeout_seconds: float = 20.0
    pdf_worker_cpu_seconds: int = 15
    pdf_worker_address_space_bytes: int = 512 * 1024 * 1024
    pdf_worker_output_bytes: int = 8 * 1024 * 1024
    # Bounded reads of derived results.
    max_snapshot_rows: int = 20
    max_resource_rows: int = 200
    max_warnings: int = 50

    def narrowed(self, **lowering: int | float) -> "IngestionLimits":
        """Return a copy where every given value is the minimum of old and new."""
        updates: dict[str, int | float] = {}
        for name, value in lowering.items():
            current = getattr(self, name)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"{name} braucht eine numerische Grenze")
            if value < 0:
                raise ValueError(f"{name} darf nicht negativ sein")
            updates[name] = min(current, value)
        return replace(self, **updates)


DEFAULT_LIMITS = IngestionLimits()


# -- error codes -------------------------------------------------------------
# Codes, not exception texts, cross the API and the database. They are stable
# identifiers for clients and tests; the message is for humans only.

CODE_SOURCE_NOT_FOUND = "source_not_found"
CODE_SOURCE_INACTIVE = "source_inactive"
CODE_SOURCE_TYPE_UNSUPPORTED = "source_type_unsupported"
CODE_SOURCE_PATH_NOT_ABSOLUTE = "source_path_not_absolute"
CODE_SOURCE_PATH_INVALID = "source_path_invalid"
CODE_SOURCE_NOT_ALLOWED = "source_not_allowed"
CODE_SOURCE_EXCLUDED = "source_excluded"
CODE_SOURCE_UNSAFE = "source_unsafe"
CODE_FILE_CHANGED = "file_changed_during_read"
CODE_FILE_SHORT_READ = "file_short_read"
CODE_FILE_TOO_LARGE = "file_too_large"
CODE_SOURCE_CHANGED = "source_changed"
CODE_SOURCE_STALE = "source_stale"
CODE_CONTEXT_NOT_FOUND = "context_not_found"
CODE_RESOURCE_NOT_FOUND = "resource_not_found"
CODE_RESOURCE_WRONG_CONTEXT = "resource_wrong_context"
CODE_RESOURCE_ARCHIVED = "resource_archived"
CODE_RESOURCE_MISSING = "resource_missing"
CODE_UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
CODE_HASH_MISMATCH = "hash_mismatch"
CODE_TRANSACTION_CONFLICT = "transaction_conflict"
CODE_PERSISTENCE_INTEGRITY = "persistence_integrity"
CODE_UNEXPECTED_INTERNAL = "unexpected_internal"


class ResourceIngestionError(Exception):
    """Base class of every refusal of the registry.

    ``code`` is the contract; ``detail`` carries bounded, non-sensitive context
    (counts, relative labels, limit names). It never carries a raw exception text
    from the filesystem and never carries an absolute path.
    """

    code = "resource_ingestion_error"
    status_code = 422

    def __init__(self, detail: str = "", *, code: str | None = None, **context: object) -> None:
        super().__init__(detail or self.code)
        if code is not None:
            self.code = code
        self.detail = detail
        self.context: dict[str, object] = dict(context)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"code": self.code}
        if self.detail:
            payload["detail"] = self.detail
        payload.update(self.context)
        return payload


class SourceNotFoundError(ResourceIngestionError):
    code = CODE_SOURCE_NOT_FOUND
    status_code = 404


class ContextNotFoundError(ResourceIngestionError):
    code = CODE_CONTEXT_NOT_FOUND
    status_code = 404


class ResourceNotFoundError(ResourceIngestionError):
    code = CODE_RESOURCE_NOT_FOUND
    status_code = 404


class SourcePathError(ResourceIngestionError):
    """A stored source path is not a usable absolute local address."""

    code = CODE_SOURCE_PATH_INVALID
    status_code = 422


class SourceAccessError(ResourceIngestionError):
    """The source exists in the database but is not readable as configured."""

    code = CODE_SOURCE_NOT_ALLOWED
    status_code = 403


class SourceUnsafeError(ResourceIngestionError):
    """A path component is a link or a special file, or containment failed."""

    code = CODE_SOURCE_UNSAFE
    status_code = 409


class SourceFileChangedError(SourceUnsafeError):
    """The file changed while it was read; the bytes are not a consistent set."""

    code = CODE_FILE_CHANGED


class SourceFileShortReadError(SourceUnsafeError):
    """The descriptor delivered fewer bytes than the file announced.

    A regular file only reports end of file at its end, so this means the bytes
    that were read are not the file -- the same consequence as a change during the
    read, but a different cause and therefore a different code. Refusing it keeps
    the hash of no bytes at all out of the registry.
    """

    code = CODE_FILE_SHORT_READ


class SourceFileTooLargeError(ResourceIngestionError):
    """The file exceeds the fixed per-file budget of 20 MiB."""

    code = CODE_FILE_TOO_LARGE
    status_code = 413


class SourceChangedError(ResourceIngestionError):
    """Source identity changed between observation and result commit."""

    code = CODE_SOURCE_CHANGED
    status_code = 409


class SourceStaleError(ResourceIngestionError):
    """Source was changed, archived or removed; access is invalidated."""

    code = CODE_SOURCE_STALE
    status_code = 409


class ResourceVersionMismatchError(ResourceIngestionError):
    """The original bytes no longer match the hash a proposal was built on."""

    code = CODE_HASH_MISMATCH
    status_code = 409


class ResourceTransactionError(ResourceIngestionError):
    """An unclean session boundary or a conflicting registry transaction."""

    code = CODE_TRANSACTION_CONFLICT
    status_code = 409


class ResourcePersistenceError(ResourceIngestionError):
    """A scan result could not satisfy a persistence constraint."""

    code = CODE_PERSISTENCE_INTEGRITY
    status_code = 500


class ResourceInternalError(ResourceIngestionError):
    """An unexpected scan failure; its original cause stays internal."""

    code = CODE_UNEXPECTED_INTERNAL
    status_code = 500


# -- pure path and classification rules ---------------------------------------


def _reject_control_characters(value: str, field: str) -> None:
    if _CONTROL_CHARACTERS.search(value):
        raise SourcePathError(
            "Pfad enthaelt Steuerzeichen", field=field, code=CODE_SOURCE_PATH_INVALID
        )


def canonical_locator(path: str, *, field: str = "path") -> str:
    """Return the canonical internal address of a local source path.

    Absolute input only. ``.``/``..`` segments are collapsed, duplicate and
    trailing separators are removed, and the result is a plain POSIX string.
    Symlinks are deliberately *not* resolved: the no-follow walk in the scanner
    rejects them, and resolving here would hide that rejection.

    The returned value is the registry's internal locator. It is never returned
    by a DTO and never accepted from a request.
    """
    if not isinstance(path, str):
        raise SourcePathError(
            "Pfad muss ein String sein", field=field, code=CODE_SOURCE_PATH_INVALID
        )
    _reject_control_characters(path, field)
    candidate = path.strip()
    if not candidate:
        raise SourcePathError(
            "Pfad darf nicht leer sein", field=field, code=CODE_SOURCE_PATH_INVALID
        )
    if not candidate.startswith("/"):
        raise SourcePathError(
            "Quellenpfade muessen absolut sein", field=field, code=CODE_SOURCE_PATH_NOT_ABSOLUTE
        )
    if len(candidate) > 4096:
        raise SourcePathError("Pfad ist zu lang", field=field, code=CODE_SOURCE_PATH_INVALID)
    normalized = posixpath.normpath(candidate)
    if not normalized.startswith("/"):
        raise SourcePathError("Pfad verlaesst das Dateisystemwurzelverzeichnis", field=field)
    return normalized


def is_within(root: str, candidate: str) -> bool:
    """True if ``candidate`` is ``root`` itself or below it, on segment bounds."""
    if candidate == root:
        return True
    prefix = root if root.endswith("/") else root + "/"
    return candidate.startswith(prefix)


def resource_type_for(name: str) -> str:
    """Classify a file name into the closed ``resources.resource_type`` set."""
    lowered = name.lower()
    if lowered.endswith(tuple(PDF_SUFFIXES)):
        return "pdf"
    if lowered.endswith(tuple(MARKDOWN_SUFFIXES)):
        return "markdown"
    if lowered.endswith(tuple(TEXT_SUFFIXES)):
        return "text"
    return "other"


def is_extractable(resource_type: str) -> bool:
    """Return whether this classified type has a supported bounded extractor."""
    return resource_type in {"pdf", "markdown", "text"}


def title_for(relative_path: str, *, fallback: str = "Dokument") -> str:
    """Derive a bounded, human readable title from a relative label."""
    name = posixpath.basename(relative_path.rstrip("/")) or fallback
    for suffix in sorted(MARKDOWN_SUFFIXES | TEXT_SUFFIXES | PDF_SUFFIXES, key=len, reverse=True):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]
            break
    name = name.replace("_", " ").strip() or fallback
    if len(name) > MAX_TITLE_LENGTH:
        name = name[:MAX_TITLE_LENGTH].rstrip()
    return name


def relative_path_for(components: Iterable[str]) -> str:
    """Build the relative, POSIX style label of an enumerated file.

    The label is what leaves the component towards models and clients. It is
    relative to the *source root*, never an absolute address.
    """
    parts = [part for part in components if part not in ("", ".")]
    if not parts:
        raise ValueError("relative_path braucht mindestens ein Segment")
    for part in parts:
        if part in ("..",) or "/" in part or _CONTROL_CHARACTERS.search(part):
            raise ValueError("unerlaubtes Pfadsegment")
    label = posixpath.join(*parts)
    if len(label) > MAX_RELATIVE_PATH_LENGTH:
        raise ValueError("relative_path ist zu lang")
    return label


def is_excluded_component(name: str) -> bool:
    """True for hidden, VCS, virtualenv and build directories."""
    if not name or name in (".", ".."):
        return True
    if name.startswith("."):
        return True
    if (
        name in VCS_DIRECTORY_NAMES
        or name in VENV_DIRECTORY_NAMES
        or name in EXCLUDED_DIRECTORY_NAMES
    ):
        return True
    lowered = name.lower()
    return lowered in VCS_DIRECTORY_NAMES or lowered in VENV_DIRECTORY_NAMES


# -- typed results -----------------------------------------------------------


class ResourceSummary(WorkspaceDTO):
    """Registry metadata of one resource. Carries no absolute address.

    ``last_seen_at`` answers "when did the owning source last see this address",
    and nothing more. It is *not* the time of a successful read: a file that was
    seen and could not be read, was empty or blew the budget has it too. Whether
    the bytes are a proven current version is ``has_content_hash`` /
    ``current_snapshot_id``, never this timestamp.
    """

    id: str
    context_id: str
    source_id: str | None = None
    title: str
    relative_path: str
    resource_type: str
    status: str
    has_content_hash: bool = False
    byte_size: int | None = Field(default=None, ge=0)
    mtime_ns: int | None = None
    current_snapshot_id: str | None = None
    last_seen_at: datetime | None = None
    updated_at: datetime | None = None


class SnapshotSummary(WorkspaceDTO):
    """One immutable extraction snapshot, including its provenance."""

    id: str
    resource_id: str
    content_hash: str
    extractor_version: str
    status: SnapshotStatus
    truncated: bool
    error_code: str | None = None
    character_count: int | None = Field(default=None, ge=0)
    page_count: int | None = Field(default=None, ge=0)
    created_at: datetime | None = None


class SnapshotDetail(SnapshotSummary):
    """A snapshot with its bounded text and extraction metadata."""

    text: str | None = Field(default=None, max_length=200_000)
    metadata_payload: dict = Field(default_factory=dict)


class ResourceDetail(WorkspaceDTO):
    """One resource with its current snapshot and a bounded snapshot history."""

    resource: ResourceSummary
    current_snapshot: SnapshotDetail | None = None
    snapshots: tuple[SnapshotSummary, ...] = ()
    # A snapshot exists, but the resource is missing/archived or the hash moved
    # on. The text is then history, never the current state of the document.
    snapshot_is_stale: bool = False
    stale_reason: str | None = None
    snapshot_count: int = 0
    snapshots_truncated: bool = False


class ResourceListPage(WorkspaceDTO):
    items: tuple[ResourceSummary, ...] = ()
    total: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)


class ScanCounters(WorkspaceDTO):
    observed: int = Field(default=0, ge=0)
    created: int = Field(default=0, ge=0)
    refreshed: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)
    marked_missing: int = Field(default=0, ge=0)
    skipped_unsafe: int = Field(default=0, ge=0)
    skipped_excluded: int = Field(default=0, ge=0)
    skipped_unsupported_type: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    #: Directory entries the enumeration charged to ``max_entries``: files,
    #: subdirectories, excluded names and unsafe objects together. It makes a
    #: truncation of the enumeration visible instead of silent.
    entries: int = Field(default=0, ge=0)
    #: Resources that had lost their source (``source_id`` NULL) and were attached
    #: to this source again by a fresh, policy-checked observation.
    rebound: int = Field(default=0, ge=0)


class ScanWarning(WorkspaceDTO):
    code: str
    count: int = Field(default=1, ge=1)
    relative_path: str | None = None


class ScanResult(WorkspaceDTO):
    """Typed outcome of one scan.

    ``complete`` answers a different question than ``status``: it says whether
    the enumeration saw the *whole* source scope. Only a complete scan may mark
    unseen resources of that source as missing.
    """

    source_id: str
    context_id: str
    status: Literal["completed", "incomplete", "failed"]
    complete: bool
    processing_run_id: str
    counters: ScanCounters = Field(default_factory=ScanCounters)
    warnings: tuple[ScanWarning, ...] = ()
    warnings_truncated: bool = False
    error_code: str | None = None


class ExtractionResult(WorkspaceDTO):
    resource_id: str
    status: SnapshotStatus
    processing_run_id: str
    snapshot_id: str | None = None
    content_hash: str | None = None
    extractor_version: str | None = None
    truncated: bool = False
    character_count: int = Field(default=0, ge=0)
    page_count: int | None = Field(default=None, ge=1)
    reused_existing_snapshot: bool = False
    error_code: str | None = None
    metadata: dict = Field(default_factory=dict)


class ResourceVersionCheck(WorkspaceDTO):
    """Result of the pre-apply freshness check. It never writes anything."""

    resource_id: str
    context_id: str
    source_id: str | None = None
    ok: bool
    expected_hash: str
    actual_hash: str | None = None
    resource_status: str
    source_status: str | None = None
    checked_at: datetime
    reason: str | None = None


def summarize_warnings(
    codes: dict[str, int], *, limit: int
) -> tuple[tuple[ScanWarning, ...], bool]:
    """Turn a code counter into a deterministic, bounded warning tuple."""
    ordered = sorted(codes.items())
    selected = ordered[:limit]
    return (
        tuple(ScanWarning(code=code, count=count) for code, count in selected),
        len(ordered) > limit,
    )
