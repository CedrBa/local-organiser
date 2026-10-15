"""Explicit extraction of one freshly authorised registered resource."""

from __future__ import annotations

from sqlalchemy.orm import Session
from organiser.core.config import Settings
from organiser.db.models import (
    ProcessingRun,
    Resource,
    utc_now,
)
from organiser.domain.resources import (
    CODE_UNSUPPORTED_MEDIA_TYPE,
    DEFAULT_LIMITS,
    ExtractionResult,
    IngestionLimits,
    ResourceIngestionError,
    SnapshotStatus,
    SourceChangedError,
    SourceStaleError,
)
from organiser.services.extraction import extract_bytes, extractor_version_for
from organiser.services.source_scanner import (
    FileRead,
    IngestionPolicy,
    read_local_file,
)
from .access import _authorize_resource_address, _load_source_for_access, _os_error_to_refusal
from .constants import (
    EVENT_RESOURCE_REFRESHED,
    REASON_RESOURCE_STATUS_ARCHIVED,
    REASON_RESOURCE_STATUS_MISSING,
    REASON_STALE_MISSING,
    RUN_TYPE_EXTRACT,
    STATUS_ACTIVE,
    STATUS_MISSING,
)
from .identity import SourceIdentity, _load_resource_identity
from .runs import (
    _audit,
    _finish_run,
    _open_run,
    _record_rejected_run,
    _record_rejection_without_run,
)
from .snapshots import _find_or_create_snapshot
from .transactions import _ResultTransaction, _end_read_transaction, _require_application_boundary


# -- extraction --------------------------------------------------------------


def _update_observed_version(
    session: Session, resource_id: str, read: FileRead, *, source: SourceIdentity
) -> None:
    """Record the freshly read version on the resource row.

    The current version is the hash of the last successful observation, so an
    extraction that finds newer bytes updates it and the previous snapshot stops
    being the current state of the document.

    A zero-length original is the one read that produces no version. The current
    hash is then cleared, for the same reason and with the same result as in
    :func:`_refresh_resource`: nobody may go on quoting the previous text as the
    current state of a document that now has none. The snapshot the caller stores
    next is the visible ``failed``/``empty_file`` record of the attempt, the
    immutable history keeps the version that really was read, and ``byte_size`` /
    ``mtime_ns`` keep describing that last measured version.
    """
    resource = session.get(Resource, resource_id)
    if resource is None:  # pragma: no cover - the re-read above guarantees existence
        return
    resource.last_seen_at = utc_now()
    if read.is_empty:
        invalidated = resource.current_content_hash is not None
        resource.current_content_hash = None
        if invalidated:
            _audit(
                session,
                event_type=EVENT_RESOURCE_REFRESHED,
                entity_type="resource",
                entity_id=resource.id,
                payload={
                    "context_id": resource.context_id,
                    "source_id": source.source_id,
                    "relative_path": resource.relative_path,
                    "byte_size": resource.byte_size,
                    "observed_during": RUN_TYPE_EXTRACT,
                    "version_invalidated": True,
                    "unread_reason": "empty_file",
                },
                source_id=source.source_id,
            )
        return
    changed = resource.current_content_hash != read.content_hash
    resource.current_content_hash = read.content_hash
    resource.byte_size = read.byte_size
    resource.mtime_ns = read.mtime_ns
    if changed:
        _audit(
            session,
            event_type=EVENT_RESOURCE_REFRESHED,
            entity_type="resource",
            entity_id=resource.id,
            payload={
                "context_id": resource.context_id,
                "source_id": source.source_id,
                "relative_path": resource.relative_path,
                "byte_size": read.byte_size,
                "observed_during": RUN_TYPE_EXTRACT,
            },
            source_id=source.source_id,
        )


def extract_resource(
    session: Session,
    resource_id: str,
    settings: Settings,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    policy: IngestionPolicy | None = None,
    worker_path=None,
    worker_timeout: float | None = None,
) -> ExtractionResult:
    """Re-read one original and store exactly one immutable snapshot of it.

    The bytes come from the descriptor based reader, so the content hash belongs
    to the original file and the extraction is a pure function of those bytes plus
    the extractor version.

    A ``(content_hash, extractor_version)`` pair that already has a snapshot is
    never written twice. That includes failed and unsupported snapshots: a
    repeated attempt is reported as ``reused_existing_snapshot`` and documented in
    the run instead of quietly replacing a visible failure. A genuinely new
    attempt needs new bytes or a new extractor version -- a code change, never a
    request parameter.
    """
    _require_application_boundary(session)
    run: ProcessingRun | None = None
    try:
        resource = _load_resource_identity(session, resource_id)
        source = _load_source_for_access(session, resource.source_id)
        effective_policy = policy or IngestionPolicy.from_settings(settings)
        _authorize_resource_address(resource, source, effective_policy)
        if resource.status != STATUS_ACTIVE:
            raise SourceStaleError(
                "Die Ressource ist nicht aktiv",
                reason=REASON_RESOURCE_STATUS_MISSING
                if resource.status == STATUS_MISSING
                else REASON_RESOURCE_STATUS_ARCHIVED,
                resource_id=resource_id,
                resource_status=resource.status,
            )
        version = extractor_version_for(resource.resource_type)
        if version is None:
            raise ResourceIngestionError(
                "Fuer diesen Ressourcentyp gibt es keinen Extraktor",
                code=CODE_UNSUPPORTED_MEDIA_TYPE,
                resource_type=resource.resource_type,
            )
        _end_read_transaction(session)
        run = _open_run(
            session,
            run_type=RUN_TYPE_EXTRACT,
            identity=source,
            resource_id=resource_id,
            extra={"extractor_version": version},
        )
        read = read_local_file(resource.locator, effective_policy, limits)
        outcome = extract_bytes(
            resource.resource_type,
            read.data,
            limits=limits,
            worker_path=worker_path,
            timeout=worker_timeout,
        )
    except (ResourceIngestionError, OSError) as raw_error:
        error = (
            raw_error
            if isinstance(raw_error, ResourceIngestionError)
            else _os_error_to_refusal(raw_error)
        )
        _end_read_transaction(session)
        if run is None:
            _record_rejection_without_run(
                session, error, run_type=RUN_TYPE_EXTRACT, resource_id=resource_id
            )
        else:
            _record_rejected_run(session, run, error, resource_id=resource_id)
        raise error from raw_error

    try:
        with _ResultTransaction(session):
            fresh = _load_resource_identity(session, resource_id)
            fresh_source = _load_source_for_access(session, fresh.source_id)
            if (
                fresh.identity_tuple() != resource.identity_tuple()
                or fresh_source.identity_tuple() != source.identity_tuple()
            ):
                raise SourceChangedError(
                    "Quelle oder Ressource wurden waehrend der Extraktion veraendert; "
                    "das Ergebnis wird verworfen"
                )
            if fresh.status != STATUS_ACTIVE:
                raise SourceStaleError(
                    "Die Ressource ist nicht mehr aktiv",
                    reason=REASON_STALE_MISSING,
                    resource_id=resource_id,
                )
            _update_observed_version(session, fresh.resource_id, read, source=fresh_source)
            snapshot, created = _find_or_create_snapshot(
                session, fresh, read.content_hash, outcome, source=fresh_source
            )
            _finish_run(
                session,
                run,
                status="completed",
                output_payload={
                    "resource_id": resource_id,
                    "content_hash": read.content_hash,
                    "extractor_version": outcome.extractor_version,
                    "status": outcome.status.value,
                    "truncated": outcome.truncated,
                    "reused_existing_snapshot": not created,
                    "error_code": outcome.error_code,
                },
            )
        return ExtractionResult(
            resource_id=resource_id,
            status=SnapshotStatus(snapshot.status),
            processing_run_id=run.id,
            snapshot_id=snapshot.id,
            content_hash=snapshot.content_hash,
            extractor_version=snapshot.extractor_version,
            truncated=snapshot.truncated,
            character_count=len(snapshot.text) if snapshot.text is not None else 0,
            page_count=(snapshot.metadata_payload or {}).get("page_count"),
            reused_existing_snapshot=not created,
            error_code=snapshot.error_code,
            metadata=snapshot.metadata_payload or {},
        )
    except (ResourceIngestionError, OSError) as raw_error:
        error = (
            raw_error
            if isinstance(raw_error, ResourceIngestionError)
            else _os_error_to_refusal(raw_error)
        )
        _record_rejected_run(session, run, error, resource_id=resource_id)
        raise error from raw_error
