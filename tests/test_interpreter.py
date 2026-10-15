from datetime import UTC, datetime
import json

import pytest
from sqlalchemy import select

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Event, Inbox, ProcessingRun, Task
from organiser.db.session import create_session_factory
from organiser.services.domain import ConflictError, create_task
from organiser.services.interpreter import interpret_inbox
from organiser.services.local_llm import InterpreterClientError


class FakeInterpreter:
    model_name = "fake-local-model"

    def __init__(self, response: str | Exception):
        self.response = response

    def interpret(self, _prompt: str) -> str:
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def session(tmp_path):
    settings = Settings(database_path=tmp_path / "interpreter.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def add_pending_inbox(session, inbox_id: str, text: str = "Bitte bearbeiten"):
    session.add(
        Inbox(
            id=inbox_id,
            created_at=datetime(2026, 9, 17, 10, 0, tzinfo=UTC),
            received_at=datetime(2026, 9, 17, 10, 1, tzinfo=UTC),
            text=text,
            source="test",
            processing_status="pending",
        )
    )
    session.commit()


def response(inbox_id: str, action: str, **fields) -> str:
    return json.dumps(
        {"source_inbox_id": inbox_id, "action": action, "missing_fields": [], **fields}
    )


def test_interpreter_creates_task_from_natural_language(session):
    add_pending_inbox(session, "inbox-task", "Ich muss Max nächste Woche anschreiben.")
    client = FakeInterpreter(
        response("inbox-task", "create_task", title="Max anschreiben", confidence=0.9)
    )

    result = interpret_inbox(session, inbox_id="inbox-task", client=client)

    assert result.status == "completed"
    task = session.get(Task, result.entity_id)
    assert task.title == "Max anschreiben"
    assert task.source_inbox_id == "inbox-task"
    event = session.scalar(select(Event).where(Event.entity_id == task.id))
    assert event.source_type == "inbox"
    assert event.source_id == "inbox-task"
    run = session.get(ProcessingRun, result.run_id)
    assert run.model == "fake-local-model"
    assert run.run_type == "local_interpreter"
    assert session.get(Inbox, "inbox-task").processing_status == "processed"


def test_interpreter_creates_project(session):
    add_pending_inbox(session, "inbox-project", "Erstelle ein Projekt fuer meine Hausarbeit.")
    result = interpret_inbox(
        session,
        inbox_id="inbox-project",
        client=FakeInterpreter(response("inbox-project", "create_project", title="Hausarbeit")),
    )

    assert result.status == "completed"
    assert result.entity_type == "project"
    assert session.get(Inbox, "inbox-project").processing_status == "processed"


def test_interpreter_applies_explicit_task_status_and_reschedule(session):
    task = create_task(session, title="Versicherung anrufen")
    add_pending_inbox(session, "inbox-complete", "Ich habe die Versicherung erledigt.")
    complete_result = interpret_inbox(
        session,
        inbox_id="inbox-complete",
        client=FakeInterpreter(
            response("inbox-complete", "complete_task", target_entity_id=task.id)
        ),
    )
    assert complete_result.status == "completed"
    assert session.get(Task, task.id).status == "done"

    add_pending_inbox(session, "inbox-reschedule", "Verschieb die Aufgabe auf Freitag.")
    reschedule_result = interpret_inbox(
        session,
        inbox_id="inbox-reschedule",
        client=FakeInterpreter(
            response(
                "inbox-reschedule",
                "reschedule_task",
                target_entity_id=task.id,
                due_at="2026-09-19T10:00:00Z",
            )
        ),
    )
    assert reschedule_result.status == "completed"
    assert session.get(Task, task.id).due_at is not None


def test_invalid_or_uncertain_output_does_not_change_domain_state(session):
    add_pending_inbox(session, "inbox-invalid")
    invalid = interpret_inbox(session, inbox_id="inbox-invalid", client=FakeInterpreter("not json"))
    assert invalid.status == "failed"
    assert session.scalars(select(Task)).all() == []
    assert session.get(Inbox, "inbox-invalid").processing_status == "pending"

    add_pending_inbox(session, "inbox-uncertain")
    uncertain = interpret_inbox(
        session,
        inbox_id="inbox-uncertain",
        client=FakeInterpreter(
            response(
                "inbox-uncertain",
                "complete_task",
                uncertainty="Task nicht eindeutig",
                missing_fields=["target_entity_id"],
            )
        ),
    )
    assert uncertain.status == "failed"
    assert session.scalars(select(Task)).all() == []
    assert session.get(ProcessingRun, uncertain.run_id).error is not None


def test_model_error_remains_retryable_without_domain_change(session):
    add_pending_inbox(session, "inbox-error")
    result = interpret_inbox(
        session,
        inbox_id="inbox-error",
        client=FakeInterpreter(InterpreterClientError("connection refused")),
    )

    assert result.status == "failed"
    assert session.scalars(select(Task)).all() == []
    inbox = session.get(Inbox, "inbox-error")
    assert inbox.processing_status == "pending"
    assert "connection refused" in inbox.last_error


def test_interpreter_is_idempotent_for_processed_inbox(session):
    add_pending_inbox(session, "inbox-once")
    client = FakeInterpreter(response("inbox-once", "create_task", title="Einmal"))
    interpret_inbox(session, inbox_id="inbox-once", client=client)

    with pytest.raises(ConflictError):
        interpret_inbox(session, inbox_id="inbox-once", client=client)
    assert len(session.scalars(select(Task)).all()) == 1
