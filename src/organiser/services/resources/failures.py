"""Classify scan failures and reconcile committed outcomes without hiding the cause."""

from __future__ import annotations

import sqlite3
import uuid
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from organiser.db.models import (
    Event,
    ProcessingRun,
    utc_now,
)
from organiser.domain.resources import (
    ResourceIngestionError,
    ResourceInternalError,
    ResourcePersistenceError,
    ResourceTransactionError,
)
from .access import _os_error_to_refusal
from .constants import (
    EVENT_SOURCE_REJECTED,
    EVENT_SOURCE_SCANNED,
    RUN_TYPE_SCAN,
    _REGISTRY_UNIQUE_MESSAGES,
    _SCAN_LOG,
)
from .identity import SourceIdentity
from .runs import _audit


def _scan_result_commit_outcome(session: Session, run_id: str) -> str:
    """Check the atomic run/event marker using a fresh database transaction.

    A failed commit acknowledgement is not proof of rollback. Only the persisted
    completed/incomplete run plus its scan event prove the result was committed;
    the original running row without that event proves it was not. Refuse to
    guess on failed reads or inconsistent markers. Never retry the result write.
    """
    try:
        session.rollback()
        with Session(bind=session.get_bind()) as observer:
            status = observer.scalar(select(ProcessingRun.status).where(ProcessingRun.id == run_id))
            scanned = observer.scalar(
                select(Event.id)
                .where(
                    Event.event_type == EVENT_SOURCE_SCANNED,
                    Event.payload["run_id"].as_string() == run_id,
                )
                .limit(1)
            )
            if status in ("completed", "incomplete") and scanned:
                return "committed"
            if status == "running" and not scanned:
                return "not_committed"
    except Exception:
        pass  # no driver text or secondary exception may replace the primary
    return "unknown"


def _classify_scan_failure(
    raw_error: Exception,
    phase: str,
    *,
    commit_outcome: str | None = None,
) -> tuple[str, ResourceIngestionError]:
    """Use driver evidence, never a generic IntegrityError, to identify a race.

    SQLite exposes columns rather than constraint names. Match its extended
    UNIQUE code and exact known column set only in the registry write phases.
    Driver text is inspected internally and is never copied to a payload/log.
    """
    if isinstance(raw_error, ResourceIngestionError):
        return "expected_refusal", raw_error
    if isinstance(raw_error, IntegrityError):
        original = raw_error.orig
        if (
            phase in ("result_transaction", "result_commit")
            and commit_outcome not in ("committed", "unknown")
            and isinstance(original, sqlite3.IntegrityError)
            and getattr(original, "sqlite_errorcode", None) == sqlite3.SQLITE_CONSTRAINT_UNIQUE
            and str(original) in _REGISTRY_UNIQUE_MESSAGES
        ):
            return "concurrent_conflict", ResourceTransactionError(
                "Das Scan-Ergebnis kollidiert mit einer konkurrierenden Registrierung; bitte erneut scannen"
            )
        return "persistence_integrity", ResourcePersistenceError(
            "Das Scan-Ergebnis konnte wegen einer Integritaetsverletzung nicht gespeichert werden"
        )
    if isinstance(raw_error, OSError) and phase in ("enumeration", "planning", "extraction"):
        # Preserve the existing filesystem refusal code/status.
        return "expected_refusal", _os_error_to_refusal(raw_error)
    return "unexpected_internal", ResourceInternalError(
        "Der Scan konnte wegen eines internen Fehlers nicht abgeschlossen werden"
    )


def _record_scan_failure(
    session: Session,
    run_id: str | None,
    identity: SourceIdentity | None,
    error: ResourceIngestionError,
    *,
    failure_class: str,
    phase: str,
    commit_outcome: str | None = None,
) -> None:
    """Terminalize before auditing; report recording failures without replacing the cause.

    These diagnostic attributes/notes are internal, not error.context/API data.
    A database that rejects the terminal write cannot guarantee a terminal row;
    the raised original error then explicitly signals that recording failed.
    No exception text, detail, SQL, locator or traceback is logged/persisted here.
    """
    metadata = {"failure_class": failure_class, "error_code": error.code, "phase": phase}
    if commit_outcome is not None:
        metadata["commit_outcome"] = commit_outcome
    error.failure_class = failure_class
    error.phase = phase
    error.processing_run_id = run_id
    error.terminalization_failed = False
    error.audit_recording_failed = False
    error.diagnostic_recording_failed = False
    error.commit_outcome = commit_outcome
    if commit_outcome in ("committed", "unknown"):
        # Clients must inspect this attempt instead of blindly repeating a write.
        error.context.update(
            processing_run_id=run_id,
            commit_outcome=commit_outcome,
            retry_safe=False,
        )
        error.detail = (
            "Das Scan-Ergebnis wurde gespeichert, aber die Antwort konnte nicht bestaetigt werden; "
            "den ProcessingRun pruefen und den Scan nicht automatisch wiederholen"
            if commit_outcome == "committed"
            else "Der Ausgang des Ergebnis-Commits ist unklar; den ProcessingRun pruefen "
            "und den Scan nicht automatisch wiederholen"
        )

    def recording_failed(kind: str) -> None:
        error.add_note(
            f"scan {kind} failed; failure_class={failure_class}; phase={phase}; error_code={error.code}"
        )
        try:
            _SCAN_LOG.error(
                "scan failure recording failed: kind=%s failure_class=%s phase=%s error_code=%s",
                kind,
                failure_class,
                phase,
                error.code,
            )
        except Exception:
            # Even a broken log handler must not replace the primary failure.
            error.add_note("scan diagnostic logging failed")

    if commit_outcome == "unknown":
        error.terminalization_skipped = True
        error.add_note("scan result commit outcome unknown; terminalization skipped")
        return

    try:
        session.rollback()
        run = session.get(ProcessingRun, run_id) if run_id is not None else None
        if run is not None and run.status in ("completed", "incomplete"):
            # A response failure is not a failed ingestion. Preserve the atomic
            # result, its counters, snapshots and success audit; append diagnostics
            # only. In particular, do not emit a contradictory rejection event.
            error.commit_outcome = "committed"
            run.output_payload = {
                **(run.output_payload or {}),
                "delivery_failure": {**metadata, "commit_outcome": "committed"},
            }
            try:
                session.commit()
            except Exception:
                error.diagnostic_recording_failed = True
                recording_failed("delivery_diagnostic")
                try:
                    session.rollback()
                except Exception:
                    recording_failed("rollback")
            return
        if commit_outcome == "committed":
            # Confirmed success must never become failure even if its subsequent
            # diagnostic read is unavailable/inconsistent.
            error.diagnostic_recording_failed = True
            recording_failed("delivery_diagnostic")
            session.rollback()
            return
        if run is None:
            run = ProcessingRun(
                id=run_id or str(uuid.uuid4()),
                run_type=RUN_TYPE_SCAN,
                status="failed",
                started_at=utc_now(),
                output_payload={
                    "context_id": identity.context_id if identity else None,
                    "source_id": identity.source_id if identity else None,
                },
            )
            session.add(run)
        # Independent of the success finalizer: a broken completion payload or
        # helper must not also break terminalization. Only whitelisted metadata
        # survives; do not merge arbitrary pre-existing output.
        run.status = "failed"
        run.finished_at = utc_now()
        run.output_payload = {
            "context_id": identity.context_id if identity else None,
            "source_id": identity.source_id if identity else None,
            "resource_id": None,
            **metadata,
        }
        run.error = error.code
        # Capture identifiers before commit/expiration.
        saved_id = run.id
        source_id = identity.source_id if identity else None
        session.commit()
        error.processing_run_id = saved_id
    except Exception:
        if commit_outcome == "committed":
            error.diagnostic_recording_failed = True
            recording_failed("delivery_diagnostic")
        else:
            error.terminalization_failed = True
            recording_failed("terminalization")
        try:
            session.rollback()
        except Exception:
            recording_failed("rollback")
        return

    try:
        _audit(
            session,
            event_type=EVENT_SOURCE_REJECTED,
            entity_type="processing_run",
            entity_id=saved_id,
            source_id=source_id,
            payload={**metadata, "run_type": RUN_TYPE_SCAN},
        )
        session.commit()
    except Exception:
        error.audit_recording_failed = True
        recording_failed("audit")
        try:
            session.rollback()
        except Exception:
            recording_failed("rollback")
