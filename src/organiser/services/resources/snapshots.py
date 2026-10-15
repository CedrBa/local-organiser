"""Read planned originals outside write transactions and persist extraction snapshots."""

from __future__ import annotations

import uuid
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Resource,
    ResourceSnapshot,
)
from organiser.domain.resources import (
    IngestionLimits,
    ResourceIngestionError,
    SnapshotStatus,
    is_extractable,
)
from organiser.services.extraction import ExtractionOutcome, extract_bytes, extractor_version_for
from organiser.services.source_scanner import (
    IngestionPolicy,
    read_local_file,
)
from .access import _read_refusal_reason
from .constants import EVENT_RESOURCE_EXTRACTED
from .identity import ResourceIdentity, SourceIdentity, _ScanState
from .runs import _audit


def _plan_extractions(
    session: Session,
    candidates: list[tuple[str, str, str, str]],
    *,
    limits: IngestionLimits,
    state: _ScanState,
) -> list[tuple[str, str, str, str]]:
    """Decide which observed documents still need an extraction.

    ``candidates`` are ``(locator, resource_type, content_hash, resource_id)`` with
    ``resource_id=""`` for a resource that does not exist yet. This runs in a read
    transaction that is closed immediately: it must not be part of the write
    transaction that follows.
    """
    pending: list[tuple[str, str, str, str]] = []
    budget = limits.max_extractions
    for locator, resource_type, content_hash, resource_id in candidates:
        if budget <= 0:
            state.warn("extraction_budget_exhausted")
            continue
        if not is_extractable(resource_type) or content_hash is None:
            continue
        version = extractor_version_for(resource_type)
        if version is None:  # pragma: no cover - is_extractable keeps this unreachable
            continue
        if resource_id:
            existing = session.scalar(
                select(ResourceSnapshot.id).where(
                    ResourceSnapshot.resource_id == resource_id,
                    ResourceSnapshot.content_hash == content_hash,
                    ResourceSnapshot.extractor_version == version,
                )
            )
            if existing is not None:
                state.extractions["reused"] = state.extractions.get("reused", 0) + 1
                continue
        pending.append((locator, resource_type, content_hash, resource_id))
        budget -= 1
    return pending


def _run_extractions(
    pending: list[tuple[str, str, str, str]],
    *,
    policy: IngestionPolicy,
    limits: IngestionLimits,
    state: _ScanState,
    worker_path=None,
    worker_timeout: float | None = None,
) -> dict[str, tuple[str, ExtractionOutcome]]:
    """Read and extract pending documents with no database transaction open.

    The second read is charged against the same scan-wide byte budget as the
    enumeration, and every refusal of it is a warning on the scan, not an
    exception: a document that cannot be read simply gets no snapshot.

    A refusal is named by :func:`_read_refusal_reason`, the same mapping the
    freshness check uses, so one file with one cause carries one code whether the
    enumeration, this read or the check was the first to see it. In particular an
    ``EACCES`` on an already-authorised address is ``file_unreadable`` here, never
    ``source_not_allowed``: an allowlist revocation never gets this far, because the
    enumeration authorises the address before it plans an extraction.

    Returns ``{locator: (content_hash, outcome)}``. The hash is the one of the
    bytes that were actually extracted, which is *not* necessarily the hash the
    enumeration saw -- a file that changed in between is extracted under its own
    version.
    """
    results: dict[str, tuple[str, ExtractionOutcome]] = {}
    for locator, resource_type, _content_hash, _resource_id in pending:
        if state.extraction_bytes >= limits.max_total_read_bytes:
            state.warn("extraction_read_budget_exhausted")
            break
        try:
            read = read_local_file(locator, policy, limits)
        except (ResourceIngestionError, OSError) as error:
            # One shared mapping, so a transient read failure inside a scan warns with
            # the same code the freshness check returns for the same file. The
            # enumeration already reported it that way for this very file.
            state.warn(_read_refusal_reason(error))
            continue
        state.extraction_bytes += read.byte_size
        outcome = extract_bytes(
            resource_type, read.data, limits=limits, worker_path=worker_path, timeout=worker_timeout
        )
        results[locator] = (read.content_hash, outcome)
    return results


def _store_snapshot(
    session: Session,
    resource: Resource,
    content_hash: str,
    outcome: ExtractionOutcome,
    *,
    source: SourceIdentity,
    state: _ScanState,
) -> ResourceSnapshot:
    """Insert one immutable snapshot and audit it."""
    snapshot = ResourceSnapshot(
        id=str(uuid.uuid4()),
        resource_id=resource.id,
        content_hash=content_hash,
        extractor_version=outcome.extractor_version,
        status=outcome.status.value,
        text=outcome.text if outcome.status == SnapshotStatus.READY else None,
        truncated=outcome.truncated,
        metadata_payload={
            "characters": outcome.character_count,
            "truncated": outcome.truncated,
            "source_id": source.source_id,
            **outcome.metadata,
        },
        error_code=outcome.error_code,
    )
    session.add(snapshot)
    state.extractions[outcome.status.value] = state.extractions.get(outcome.status.value, 0) + 1
    state.extracted_hashes[content_hash] = snapshot.id
    _audit(
        session,
        event_type=EVENT_RESOURCE_EXTRACTED,
        entity_type="resource",
        entity_id=resource.id,
        payload={
            "snapshot_id": snapshot.id,
            "content_hash": content_hash,
            "extractor_version": outcome.extractor_version,
            "status": outcome.status.value,
            "truncated": outcome.truncated,
            "error_code": outcome.error_code,
            "character_count": outcome.character_count,
        },
        source_id=source.source_id,
    )
    return snapshot


def _find_or_create_snapshot(
    session: Session,
    resource: ResourceIdentity,
    content_hash: str,
    outcome: ExtractionOutcome,
    *,
    source: SourceIdentity,
) -> tuple[ResourceSnapshot, bool]:
    """Return the snapshot for ``(resource, hash, version)`` and whether it was created.

    A pair that already exists is returned unchanged -- including a failed or
    unsupported snapshot. Silently writing a second row for the same version would
    either hide a failure or inflate the version space on every retry.
    """
    existing = session.scalar(
        select(ResourceSnapshot).where(
            ResourceSnapshot.resource_id == resource.resource_id,
            ResourceSnapshot.content_hash == content_hash,
            ResourceSnapshot.extractor_version == outcome.extractor_version,
        )
    )
    if existing is not None:
        return existing, False
    snapshot = ResourceSnapshot(
        id=str(uuid.uuid4()),
        resource_id=resource.resource_id,
        content_hash=content_hash,
        extractor_version=outcome.extractor_version,
        status=outcome.status.value,
        text=outcome.text if outcome.status == SnapshotStatus.READY else None,
        truncated=outcome.truncated,
        metadata_payload={
            "characters": outcome.character_count,
            "truncated": outcome.truncated,
            "source_id": source.source_id,
            **outcome.metadata,
        },
        error_code=outcome.error_code,
    )
    session.add(snapshot)
    session.flush()
    _audit(
        session,
        event_type=EVENT_RESOURCE_EXTRACTED,
        entity_type="resource",
        entity_id=resource.resource_id,
        payload={
            "snapshot_id": snapshot.id,
            "content_hash": content_hash,
            "extractor_version": outcome.extractor_version,
            "status": outcome.status.value,
            "truncated": outcome.truncated,
            "error_code": outcome.error_code,
            "character_count": outcome.character_count,
        },
        source_id=source.source_id,
    )
    return snapshot, True
