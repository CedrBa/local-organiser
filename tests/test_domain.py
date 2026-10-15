from datetime import UTC, datetime, timedelta

import pytest

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import upgrade_database
from organiser.db.models import Inbox, Project, Task
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
    cancel_task,
    complete_project,
    complete_task,
    create_project,
    create_task,
    list_child_tasks,
    list_events_after_id,
    list_events_by_correlation_id,
    list_events_for_entity,
    list_project_tasks,
    list_projects,
    list_tasks,
    mark_task_waiting,
    pause_project,
    reopen_project,
    reopen_task,
    reschedule_task,
    resume_project,
    start_task,
    update_project,
    update_task_details,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "domain.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def timestamp(hours: int = 0) -> datetime:
    return datetime(2026, 9, 17, 10, 0, tzinfo=UTC) + timedelta(hours=hours)


def test_project_lifecycle_and_events(session):
    project = create_project(
        session, title="Haus", context=EventContext(correlation_id="project-flow")
    )
    assert project.status == "active"
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="project", entity_id=project.id)
    ] == ["PROJECT_CREATED"]

    update_project(session, project_id=project.id, title="Hausrenovierung", description="Kueche")
    pause_project(session, project_id=project.id)
    assert resume_project(session, project_id=project.id).status == "active"
    assert complete_project(session, project_id=project.id).status == "completed"
    assert reopen_project(session, project_id=project.id).status == "active"

    event_types = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="project", entity_id=project.id)
    ]
    assert event_types == [
        "PROJECT_CREATED",
        "PROJECT_UPDATED",
        "PROJECT_PAUSED",
        "PROJECT_RESUMED",
        "PROJECT_COMPLETED",
        "PROJECT_REOPENED",
    ]
    assert list_events_by_correlation_id(session, "project-flow")[0].event_type == "PROJECT_CREATED"
    assert [item.id for item in list_projects(session, status="active")] == [project.id]


def test_project_rejects_invalid_transition_and_open_tasks(session):
    project = create_project(session, title="Projekt")
    with pytest.raises(ConflictError):
        resume_project(session, project_id=project.id)

    task = create_task(session, title="Offen", project_id=project.id)
    with pytest.raises(ConflictError):
        complete_project(session, project_id=project.id)
    assert session.get(Project, project.id).status == "active"

    cancel_task(session, task_id=task.id)
    assert complete_project(session, project_id=project.id).status == "completed"


def test_task_lifecycle_updates_completed_at_and_history(session):
    task = create_task(session, title="Steuer", context=EventContext(correlation_id="task-flow"))
    assert start_task(session, task_id=task.id).status == "in_progress"
    assert mark_task_waiting(session, task_id=task.id).status == "waiting"
    completed = complete_task(session, task_id=task.id)
    assert completed.completed_at is not None
    reopened = reopen_task(session, task_id=task.id)
    assert reopened.status == "open"
    assert reopened.completed_at is None
    assert cancel_task(session, task_id=task.id).status == "cancelled"

    event_types = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="task", entity_id=task.id)
    ]
    assert event_types == [
        "TASK_CREATED",
        "TASK_STARTED",
        "TASK_WAITING",
        "TASK_COMPLETED",
        "TASK_REOPENED",
        "TASK_CANCELLED",
    ]
    assert list_events_by_correlation_id(session, "task-flow")[0].event_type == "TASK_CREATED"


def test_task_validation_and_rescheduling(session):
    with pytest.raises(DomainValidationError):
        create_task(session, title=" ")
    with pytest.raises(DomainValidationError):
        create_task(session, title="Kurz", estimated_minutes=0)
    with pytest.raises(DomainValidationError):
        create_task(session, title="Termin", earliest_start_at=timestamp(2), due_at=timestamp(1))
    with pytest.raises(NotFoundError):
        create_task(session, title="Unbekannt", project_id="missing")

    task = create_task(
        session,
        title="Termin",
        earliest_start_at=timestamp(),
        due_at=timestamp(2),
        estimated_minutes=30,
    )
    changed = reschedule_task(session, task_id=task.id, due_at=timestamp(3))
    assert changed.due_at == timestamp(3)
    with pytest.raises(DomainValidationError):
        reschedule_task(
            session, task_id=task.id, earliest_start_at=timestamp(4), due_at=timestamp(3)
        )
    event = list_events_for_entity(session, entity_type="task", entity_id=task.id)[-1]
    assert event.event_type == "TASK_RESCHEDULED"
    assert event.payload["changes"]["due_at"]["old"] == timestamp(2).isoformat()


def test_subtask_inheritance_and_cycle_protection(session):
    project = create_project(session, title="Familie")
    parent = create_task(session, title="Eltern", project_id=project.id)
    child = create_task(session, title="Kind", parent_task_id=parent.id)
    assert child.project_id == project.id
    assert [item.id for item in list_child_tasks(session, parent.id)] == [child.id]
    assert [item.id for item in list_project_tasks(session, project.id)] == [parent.id, child.id]

    with pytest.raises(DomainValidationError):
        update_task_details(session, task_id=parent.id, parent_task_id=parent.id)
    with pytest.raises(DomainValidationError):
        update_task_details(session, task_id=parent.id, parent_task_id=child.id)


def test_task_project_and_inbox_references_are_validated(session):
    project = create_project(session, title="Arbeit")
    other_project = create_project(session, title="Privat")
    parent = create_task(session, title="Parent", project_id=project.id)
    with pytest.raises(DomainValidationError):
        create_task(
            session, title="Falsches Child", parent_task_id=parent.id, project_id=other_project.id
        )
    with pytest.raises(NotFoundError):
        create_task(session, title="Unbekannte Inbox", source_inbox_id="missing")

    session.add(
        Inbox(
            id="inbox-1",
            created_at=timestamp(),
            received_at=timestamp(),
            text="Inbox",
            source="test",
            processing_status="pending",
        )
    )
    session.commit()
    task = create_task(session, title="Mit Inbox", source_inbox_id="inbox-1")
    assert task.source_inbox_id == "inbox-1"


def test_pending_inbox_entries_remain_untouched_by_domain_operations(session):
    session.add(
        Inbox(
            id="pending-inbox",
            created_at=timestamp(),
            received_at=timestamp(),
            text="Nicht verarbeiten",
            source="test",
            processing_status="pending",
        )
    )
    session.commit()
    create_project(session, title="Nur Domain-State")
    inbox = session.get(Inbox, "pending-inbox")
    assert inbox.processing_status == "pending"
    assert inbox.processed_at is None


def test_updates_record_only_actual_changes_and_invalid_transition_no_event(session):
    task = create_task(session, title="Alt", description="Text", estimated_minutes=20)
    event_count = len(list_events_for_entity(session, entity_type="task", entity_id=task.id))
    update_task_details(session, task_id=task.id, title="Neu", estimated_minutes=25)
    event = list_events_for_entity(session, entity_type="task", entity_id=task.id)[-1]
    assert set(event.payload["changes"]) == {"title", "estimated_minutes"}

    with pytest.raises(ConflictError):
        mark_task_waiting(session, task_id=task.id)
    assert (
        len(list_events_for_entity(session, entity_type="task", entity_id=task.id))
        == event_count + 1
    )


def test_commit_failure_rolls_back_domain_change_and_event(session, monkeypatch):
    task = create_task(session, title="Atomar")
    original_commit = session.commit

    def failed_commit():
        raise RuntimeError("event write failed")

    monkeypatch.setattr(session, "commit", failed_commit)
    with pytest.raises(RuntimeError):
        start_task(session, task_id=task.id)
    monkeypatch.setattr(session, "commit", original_commit)

    session.expire_all()
    assert session.get(Task, task.id).status == "open"
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="task", entity_id=task.id)
    ] == ["TASK_CREATED"]


def test_task_queries_and_events_after_id(session):
    project = create_project(session, title="Queries")
    first = create_task(session, title="Ohne Deadline", project_id=project.id)
    second = create_task(session, title="Mit Deadline", project_id=project.id, due_at=timestamp(2))
    start_task(session, task_id=second.id, context=EventContext(correlation_id="query-run"))

    assert [task.id for task in list_tasks(session, project_id=project.id)] == [second.id, first.id]
    assert [task.id for task in list_tasks(session, has_deadline=True)] == [second.id]
    assert [task.id for task in list_tasks(session, status="in_progress")] == [second.id]
    assert [
        task.id for task in list_tasks(session, due_from=timestamp(1), due_to=timestamp(3))
    ] == [second.id]
    events = list_events_by_correlation_id(session, "query-run")
    assert [event.event_type for event in events] == ["TASK_STARTED"]
    assert all(event.id > 0 for event in list_events_after_id(session, 0))


def test_domain_api_uses_services_and_maps_errors(tmp_path):
    settings = make_settings(tmp_path, "api.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        created = client.post(
            "/projects", json={"title": "API", "event_context": {"correlation_id": "api-1"}}
        )
        assert created.status_code == 201
        project_id = created.json()["id"]
        task = client.post("/tasks", json={"title": "API Task", "project_id": project_id})
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert client.post(f"/projects/{project_id}/complete").status_code == 409
        assert client.post(f"/tasks/{task_id}/start").json()["status"] == "in_progress"
        assert client.get(f"/tasks/{task_id}/events").json()[-1]["event_type"] == "TASK_STARTED"
        assert (
            client.get("/events", params={"correlation_id": "api-1"}).json()[0]["event_type"]
            == "PROJECT_CREATED"
        )
        assert client.patch(f"/tasks/{task_id}", json={"estimated_minutes": 0}).status_code == 422
