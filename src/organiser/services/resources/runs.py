"""Processing-run lifecycle and correlated resource audit records."""

from __future__ import annotations

import uuid
from sqlalchemy.orm import Session
from organiser.db.models import (
    Event,
    ProcessingRun,
    utc_now,
)
from organiser.domain.resources import (
    ResourceIngestionError,
)
from .constants import EVENT_SOURCE_REJECTED
from .identity import SourceIdentity


def _audit(
    session: Session,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict,
    source_id: str | None = None,
) -> None:
    """Append one audit event.

    Payloads carry ids, relative labels, hashes, counts and codes. They never
    carry an absolute path, because an event can end up in a workspace packet
    that a model reads.
    """
    session.add(
        Event(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            source_type="context_source" if source_id else None,
            source_id=source_id,
            payload=payload,
        )
    )


def _open_run(
    session: Session,
    *,
    run_type: str,
    identity: SourceIdentity | None,
    resource_id: str | None,
    extra: dict | None = None,
    run_id: str | None = None,
) -> ProcessingRun:
    """Create and commit a ``running`` ProcessingRun in its own short transaction.

    The row is written before the filesystem work, so an aborted scan or a killed
    parser stays visible as an attempt instead of vanishing without a trace.
    """
    run = ProcessingRun(
        id=run_id or str(uuid.uuid4()),
        run_type=run_type,
        status="running",
        started_at=utc_now(),
        output_payload={
            "context_id": identity.context_id if identity else None,
            "source_id": identity.source_id if identity else None,
            "resource_id": resource_id,
            **(extra or {}),
        },
    )
    session.add(run)
    session.commit()
    return run


def _finish_run(
    session: Session,
    run: ProcessingRun,
    *,
    status: str,
    output_payload: dict | None = None,
    error: str | None = None,
) -> None:
    """Complete a run inside the caller's transaction; it commits with it."""
    run.status = status
    run.finished_at = utc_now()
    if output_payload is not None:
        run.output_payload = {**(run.output_payload or {}), **output_payload}
    if error is not None:
        run.error = error[:2000]


def _record_rejected_run(
    session: Session, run: ProcessingRun, error: ResourceIngestionError, **output: object
) -> None:
    """Persist a refused attempt, then leave the session clean again."""
    try:
        _finish_run(
            session,
            run,
            status="failed",
            output_payload={"error_code": error.code, **output},
            error=f"{error.code}: {error.detail}"[:2000],
        )
        _audit(
            session,
            event_type=EVENT_SOURCE_REJECTED,
            entity_type="processing_run",
            entity_id=run.id,
            payload={"error_code": error.code, "run_type": run.run_type, **output},
            source_id=output.get("source_id") if isinstance(output.get("source_id"), str) else None,
        )
        session.commit()
    except Exception:  # pragma: no cover - the refusal itself is the answer
        session.rollback()


def _record_rejection_without_run(
    session: Session, error: ResourceIngestionError, *, run_type: str, **output: object
) -> None:
    """Record a refusal that happened before a run row could be created."""
    try:
        run = ProcessingRun(
            id=str(uuid.uuid4()),
            run_type=run_type,
            status="failed",
            started_at=utc_now(),
            finished_at=utc_now(),
            output_payload={"error_code": error.code, **output},
            error=f"{error.code}: {error.detail}"[:2000],
        )
        session.add(run)
        session.commit()
    except Exception:  # pragma: no cover - the refusal itself is the answer
        session.rollback()
