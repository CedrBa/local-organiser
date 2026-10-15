"""Failure codes contracts for ingestion."""

from __future__ import annotations

import json
import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from organiser.api.resources import _call
from organiser.db.models import Resource
from organiser.domain.resources import (
    ResourceIngestionError,
    SourceAccessError,
    SourceChangedError,
    SourceStaleError,
)
from organiser.services.resources import scan as resources
from support.ingestion import session_for
from support.workspace_ingestion_failure_classification import (
    SECRET as SECRET,
    PHASES as PHASES,
    course as course,
    assert_safe as assert_safe,
    assert_failed as assert_failed,
    assert_committed_result as assert_committed_result,
)


@pytest.mark.parametrize("hook,phase", PHASES)
@pytest.mark.parametrize("error_type", [SourceAccessError, SourceChangedError, SourceStaleError])
def test_expected_domain_refusals_keep_code_status_and_terminalize(
    course, monkeypatch, caplog, hook, phase, error_type
):
    settings, source_id = course
    original = getattr(resources, hook)
    domain_error = error_type("Sichere bestehende Ablehnung", reason="test_refusal")

    def refusing(*args, **kwargs):
        if hook == "_audit" and kwargs.get("event_type") != resources.EVENT_SOURCE_SCANNED:
            return original(*args, **kwargs)
        raise domain_error

    monkeypatch.setattr(resources, hook, refusing)
    with session_for(settings) as session:
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        assert raised.value is domain_error
        assert raised.value.code == error_type.code
        assert raised.value.status_code == error_type.status_code
        assert raised.value.context == {"reason": "test_refusal"}
        assert_failed(session, raised.value, "expected_refusal", phase)
        assert_safe(session, raised.value, settings, caplog)


@pytest.mark.parametrize("hook,phase", PHASES)
def test_unexpected_exceptions_in_every_phase_are_safe_and_terminal(
    course, monkeypatch, caplog, hook, phase
):
    settings, source_id = course
    original = getattr(resources, hook)
    primary = RuntimeError(SECRET)

    def crashing(*args, **kwargs):
        if hook == "_audit" and kwargs.get("event_type") != resources.EVENT_SOURCE_SCANNED:
            return original(*args, **kwargs)
        raise primary

    monkeypatch.setattr(resources, hook, crashing)
    with session_for(settings) as session:
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert error.__cause__ is primary
        assert (error.status_code, error.code) == (500, "unexpected_internal")
        assert_failed(session, error, "unexpected_internal", phase)
        assert list(session.scalars(select(Resource))) == []
        assert_safe(session, error, settings, caplog)
        session.rollback()
        with pytest.raises(HTTPException) as response:
            _call(resources.scan_source, session, source_id, settings)
        assert response.value.status_code == 500
        assert SECRET not in json.dumps(response.value.detail)


@pytest.mark.parametrize("constraint", ["check", "fk", "foreign_unique", "audit"])
def test_other_real_integrity_constraints_are_not_concurrency(
    course, monkeypatch, caplog, constraint
):
    settings, source_id = course
    real_apply = resources._apply_observations
    real_audit = resources._audit

    def invalid_result(session, *args, **kwargs):
        result = real_apply(session, *args, **kwargs)
        row = session.scalar(select(Resource))
        if constraint == "check":
            row.status = SECRET
        elif constraint == "fk":
            row.context_id = SECRET
        else:
            session.execute(
                text(
                    "INSERT INTO context_sources (id, context_id, source_type, path, status, created_at, updated_at) SELECT :id, context_id, source_type, path, status, created_at, updated_at FROM context_sources"
                ),
                {"id": "other-source"},
            )
        session.flush()
        return result

    def failing_audit(session, **kwargs):
        real_audit(session, **kwargs)
        if kwargs["event_type"] == resources.EVENT_SOURCE_SCANNED:
            session.flush()

    with session_for(settings) as session:
        if constraint == "audit":
            # Reject both success and refusal audits. The refusal audit must not
            # undo the separately committed terminal run or hide the first error.
            session.execute(
                text(
                    "CREATE TRIGGER reject_scan_audit BEFORE INSERT ON events WHEN NEW.event_type IN ('CONTEXT_SOURCE_SCANNED', 'CONTEXT_SOURCE_REJECTED') BEGIN SELECT RAISE(ABORT, 'SELECT private FROM /tmp/private/source.md'); END"
                )
            )
            session.commit()
            monkeypatch.setattr(resources, "_audit", failing_audit)
        else:
            monkeypatch.setattr(resources, "_apply_observations", invalid_result)
        with pytest.raises(ResourceIngestionError) as raised:
            resources.scan_source(session, source_id, settings)
        error = raised.value
        assert isinstance(error.__cause__, IntegrityError)
        assert (error.status_code, error.code) == (500, "persistence_integrity")
        assert_failed(
            session,
            error,
            "persistence_integrity",
            "audit" if constraint == "audit" else "result_transaction",
        )
        assert error.audit_recording_failed is (constraint == "audit")
        assert list(session.scalars(select(Resource))) == []
        assert_safe(session, error, settings, caplog)


def test_message_alone_is_not_proof_of_registry_unique_conflict():
    fake = IntegrityError(
        SECRET, {}, Exception("UNIQUE constraint failed: resources.context_id, resources.locator")
    )
    failure_class, error = resources._classify_scan_failure(fake, "result_transaction")
    assert failure_class == "persistence_integrity"
    assert error.code == "persistence_integrity"
