"""Coordinate one scan while keeping observation separate from result persistence."""

from __future__ import annotations

import uuid
from sqlalchemy.orm import Session
from organiser.core.config import Settings
from organiser.domain.resources import (
    DEFAULT_LIMITS,
    IngestionLimits,
    ResourceTransactionError,
    ScanResult,
    SourceChangedError,
    summarize_warnings,
)
from organiser.services.source_scanner import (
    IngestionPolicy,
    scan_source_tree,
)
from .constants import EVENT_SOURCE_SCANNED, RUN_TYPE_SCAN
from .failures import _classify_scan_failure, _record_scan_failure, _scan_result_commit_outcome
from .identity import SourceIdentity, _load_source_identity
from .observations import _apply_observations, _plan_observations
from .runs import _audit, _finish_run, _open_run
from .snapshots import _run_extractions
from .transactions import _end_read_transaction, _require_application_boundary


def scan_source(
    session: Session,
    source_id: str,
    settings: Settings,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    policy: IngestionPolicy | None = None,
    worker_path=None,
    worker_timeout: float | None = None,
) -> ScanResult:
    """Scan one source: register its files and read bounded text from them.

    Returns a :class:`ScanResult`. ``complete`` answers "did the enumeration see
    the whole scope"; it is the only precondition for marking resources of this
    source as missing. A refusal raises a :class:`ResourceIngestionError` with a
    stable code. Both outcomes leave a ``ProcessingRun`` behind.

    The phases, and why they are in this order:

    1. read the source identity and authorise the address, then close the
       transaction -- nothing is read from disk while a transaction is open;
    2. write a ``running`` ProcessingRun, so an aborted scan stays visible;
    3. enumerate and hash the tree (no transaction, no lock);
    4. plan the write in a read transaction and close it again;
    5. read and parse the documents that need an extraction (no transaction, no
       lock -- a PDF parser may take 20 seconds per file);
    6. re-read the source identity and write everything in one short transaction.

    The signature is ``(session, source_id, settings)`` on purpose: no path, no
    limit and no flag arrives from the request. Budgets can only be lowered by an
    embedding application, never raised by a client.
    """
    identity: SourceIdentity | None = None
    run_id: str | None = None
    phase = "preflight"
    expect = "dir"
    try:
        _require_application_boundary(session)
        identity = _load_source_identity(session, source_id)
        expect = "dir" if identity.source_type == "folder" else "file"
        effective_policy = policy or IngestionPolicy.from_settings(settings)
        effective_policy.authorize(identity.locator, expect=expect)
        _end_read_transaction(session)
        phase = "run_opening"
        # Retain identity even if commit persisted but its acknowledgement failed.
        run_id = str(uuid.uuid4())
        run = _open_run(
            session, run_type=RUN_TYPE_SCAN, identity=identity, resource_id=None, run_id=run_id
        )
        phase = "enumeration"
        tree = scan_source_tree(identity.locator, effective_policy, limits, expect=expect)
        phase = "planning"
        state, pending = _plan_observations(session, identity, tree, limits=limits)
        _end_read_transaction(session)
        phase = "extraction"
        extractions = _run_extractions(
            pending,
            policy=effective_policy,
            limits=limits,
            state=state,
            worker_path=worker_path,
            worker_timeout=worker_timeout,
        )
        phase = "result_transaction"
        fresh = _load_source_identity(session, source_id)
        if fresh.identity_tuple() != identity.identity_tuple():
            raise SourceChangedError(
                "Die Quelle wurde waehrend des Scans veraendert; das Ergebnis wird verworfen",
                source_id=source_id,
            )
        state = _apply_observations(
            session, fresh, tree, state, extractions, policy=effective_policy
        )
        phase = "run_completion"
        _finish_run(
            session,
            run,
            status="completed" if tree.complete else "incomplete",
            output_payload={
                "complete": tree.complete,
                "stop_reason": tree.stop_reason,
                "counters": state.counters,
                "extractions": state.extractions,
                "warnings": state.warnings,
            },
            error=tree.stop_reason,
        )
        phase = "audit"
        _audit(
            session,
            event_type=EVENT_SOURCE_SCANNED,
            entity_type="context_source",
            entity_id=fresh.source_id,
            payload={
                "run_id": run.id,
                "complete": tree.complete,
                "stop_reason": tree.stop_reason,
                "counters": state.counters,
                "extractions": state.extractions,
            },
            source_id=fresh.source_id,
        )
        phase = "result_commit"
        session.commit()
        phase = "response"
        warnings, warnings_truncated = summarize_warnings(state.warnings, limit=limits.max_warnings)
        return ScanResult(
            source_id=fresh.source_id,
            context_id=fresh.context_id,
            status="completed" if tree.complete else "incomplete",
            complete=tree.complete,
            processing_run_id=run.id,
            counters=state.counters_dto(),
            warnings=warnings,
            warnings_truncated=warnings_truncated,
            error_code=tree.stop_reason,
        )
    except Exception as raw_error:
        commit_outcome = (
            "committed"
            if phase == "response"
            else _scan_result_commit_outcome(session, run_id)
            if phase == "result_commit"
            else None
        )
        failure_class, error = _classify_scan_failure(
            raw_error, phase, commit_outcome=commit_outcome
        )
        # Do not roll back work owned by the caller at an unclean boundary.
        if phase != "preflight" or not isinstance(error, ResourceTransactionError):
            _record_scan_failure(
                session,
                run_id,
                identity,
                error,
                failure_class=failure_class,
                phase=phase,
                commit_outcome=commit_outcome,
            )
        if error is raw_error:
            raise
        raise error from raw_error
