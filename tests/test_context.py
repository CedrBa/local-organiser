from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Event, Inbox
from organiser.db.session import create_session_factory
from organiser.services.context import project_context, task_context
from organiser.services.domain import create_project, create_task, start_task


@pytest.fixture
def session(tmp_path):
    settings = Settings(database_path=tmp_path / "context.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def test_task_context_contains_hierarchy_project_events_and_inbox(session):
    session.add(
        Inbox(
            id="inbox-context",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            text="Ursprung",
            source="test",
            processing_status="pending",
        )
    )
    session.commit()
    project = create_project(session, title="Hausarbeit")
    parent = create_task(
        session, title="Kapitel schreiben", project_id=project.id, source_inbox_id="inbox-context"
    )
    child = create_task(session, title="Gliederung", parent_task_id=parent.id)
    related = create_task(session, title="Recherche", project_id=project.id)
    other_project = create_project(session, title="Privat")
    other = create_task(session, title="Irrelevant", project_id=other_project.id)
    start_task(session, task_id=parent.id)

    packet = task_context(session, child.id)

    assert packet.target["id"] == child.id
    assert packet.parent_task["id"] == parent.id
    assert packet.project["id"] == project.id
    assert {task["id"] for task in packet.related_tasks} == {parent.id, related.id}
    assert other.id not in {task["id"] for task in packet.related_tasks}
    assert packet.source_inbox is None
    parent_packet = task_context(session, parent.id)
    assert parent_packet.source_inbox["id"] == "inbox-context"
    assert any(event["event_type"] == "TASK_STARTED" for event in packet.recent_events)
    assert packet.context_event_max_id == session.scalar(
        select(Event.id).order_by(Event.id.desc()).limit(1)
    )


def test_project_context_is_bounded_deterministic_and_excludes_other_projects(session):
    project = create_project(session, title="Plan")
    other_project = create_project(session, title="Andere")
    base = datetime(2026, 9, 17, 10, tzinfo=UTC)
    for number in range(14):
        create_task(
            session,
            title=f"Task {number:02}",
            project_id=project.id,
            due_at=base + timedelta(days=number),
        )
    other = create_task(session, title="Nicht im Kontext", project_id=other_project.id)

    first = project_context(session, project.id)
    second = project_context(session, project.id)

    assert len(first.related_tasks) == 12
    assert [task["id"] for task in first.related_tasks] == [
        task["id"] for task in second.related_tasks
    ]
    assert other.id not in {task["id"] for task in first.related_tasks}
    assert len(first.recent_events) <= first.selection["max_recent_events"]
    assert first.context_event_max_id == second.context_event_max_id
