from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Event, Inbox, ProcessingRun, Task
from organiser.db.session import create_session_factory
from organiser.domain.proposals import InterpretationProposal
from organiser.services.domain import ConflictError
from organiser.services.triage import apply_manual_proposal


@pytest.fixture
def session(tmp_path):
    settings = Settings(database_path=tmp_path / "triage.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def add_pending_inbox(session, inbox_id: str = "inbox-triage") -> Inbox:
    inbox = Inbox(
        id=inbox_id,
        created_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
        received_at=datetime(2026, 9, 17, 10, 1, tzinfo=UTC),
        text="Bitte Aufgabe anlegen",
        source="test",
        processing_status="pending",
    )
    session.add(inbox)
    session.commit()
    return inbox


def test_manual_create_task_proposal_uses_domain_and_tracks_history(session):
    add_pending_inbox(session)
    proposal = InterpretationProposal(
        source_inbox_id="inbox-triage",
        action="create_task",
        title="Versicherung anrufen",
        confidence=0.9,
        correlation_id="triage-correlation",
    )

    result = apply_manual_proposal(session, proposal)

    assert result.status == "completed"
    task = session.get(Task, result.entity_id)
    assert task is not None
    assert task.source_inbox_id == "inbox-triage"
    event = session.scalar(select(Event).where(Event.entity_id == task.id))
    assert event.event_type == "TASK_CREATED"
    assert event.source_type == "inbox"
    assert event.source_id == "inbox-triage"
    assert event.correlation_id == "triage-correlation"
    run = session.get(ProcessingRun, result.run_id)
    assert run.status == "completed"
    assert run.output_payload["action"] == "create_task"
    assert session.get(Inbox, "inbox-triage").processing_status == "processed"


def test_invalid_proposal_is_recorded_without_domain_change(session):
    add_pending_inbox(session)
    proposal = InterpretationProposal(
        source_inbox_id="inbox-triage", action="create_task", uncertainty="Titel fehlt"
    )

    result = apply_manual_proposal(session, proposal)

    assert result.status == "failed"
    assert session.scalars(select(Task)).all() == []
    run = session.get(ProcessingRun, result.run_id)
    assert run.error is not None
    inbox = session.get(Inbox, "inbox-triage")
    assert inbox.processing_status == "pending"
    assert inbox.processed_at is None
    assert inbox.last_error is not None


def test_domain_validation_failure_is_recorded_and_inbox_remains_retryable(session):
    add_pending_inbox(session)
    proposal = InterpretationProposal(
        source_inbox_id="inbox-triage",
        action="create_task",
        title="Task mit ungueltigem Project",
        project_id="missing-project",
    )

    result = apply_manual_proposal(session, proposal)

    assert result.status == "failed"
    assert session.scalars(select(Task)).all() == []
    assert session.get(Inbox, "inbox-triage").processing_status == "pending"
    assert session.get(ProcessingRun, result.run_id).status == "failed"


def test_processed_inbox_cannot_create_duplicate_tasks(session):
    add_pending_inbox(session)
    proposal = InterpretationProposal(
        source_inbox_id="inbox-triage", action="create_task", title="Einmal"
    )
    apply_manual_proposal(session, proposal)

    with pytest.raises(ConflictError):
        apply_manual_proposal(session, proposal)
    assert len(session.scalars(select(Task)).all()) == 1
