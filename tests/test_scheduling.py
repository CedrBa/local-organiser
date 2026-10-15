from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import upgrade_database
from organiser.db.session import create_session_factory
from organiser.services.domain import create_task, update_task_details
from organiser.services.scheduling import (
    AvailabilityWindow,
    SchedulingValidationError,
    StaticAvailabilityProvider,
    create_planning_block,
    list_planning_blocks,
    normalize_availability,
    task_scheduling_info,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "scheduling.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, tzinfo=UTC)


def test_availability_is_normalized_and_static_provider_is_queryable():
    windows = normalize_availability(
        [
            AvailabilityWindow(starts_at=at(16), ends_at=at(18)),
            AvailabilityWindow(starts_at=at(14), ends_at=at(16)),
            AvailabilityWindow(starts_at=at(15), ends_at=at(17)),
        ]
    )
    provider = StaticAvailabilityProvider(windows)

    assert windows == [AvailabilityWindow(starts_at=at(14), ends_at=at(18))]
    assert provider.availability_between(at(15), at(17)) == [
        AvailabilityWindow(starts_at=at(15), ends_at=at(17))
    ]


def test_invalid_availability_is_rejected():
    with pytest.raises(ValidationError):
        AvailabilityWindow(starts_at=datetime(2026, 9, 21, 14), ends_at=at(15))
    with pytest.raises(ValidationError):
        AvailabilityWindow(starts_at=at(16), ends_at=at(16))


def test_planning_blocks_are_saved_read_and_preserve_task_scheduling_fields(session):
    task = create_task(
        session,
        title="Bericht schreiben",
        earliest_start_at=at(10),
        due_at=at(18),
        estimated_minutes=120,
        allow_split=True,
    )
    block = create_planning_block(
        session, task_id=task.id, starts_at=at(14), ends_at=at(15), source_plan_id="proposal-1"
    )

    assert list_planning_blocks(session, task_id=task.id) == [block]
    info = task_scheduling_info(session, task.id)
    assert info.earliest_start_at == at(10)
    assert info.due_at == at(18)
    assert info.estimated_minutes == 120
    assert info.allow_split is True
    assert info.planning_blocks[0].source_plan_id == "proposal-1"


def test_planning_blocks_reject_overlap_boundaries_and_unapproved_split(session):
    task = create_task(session, title="Ein Block", earliest_start_at=at(10), due_at=at(18))
    create_planning_block(session, task_id=task.id, starts_at=at(12), ends_at=at(13))
    with pytest.raises(SchedulingValidationError, match="Aufteilung"):
        create_planning_block(session, task_id=task.id, starts_at=at(13), ends_at=at(14))

    other = create_task(session, title="Anderer Task")
    with pytest.raises(SchedulingValidationError, match="ueberschneidet"):
        create_planning_block(session, task_id=other.id, starts_at=at(12, 30), ends_at=at(13, 30))
    with pytest.raises(SchedulingValidationError, match="earliest_start_at"):
        create_planning_block(session, task_id=task.id, starts_at=at(9), ends_at=at(10))
    with pytest.raises(SchedulingValidationError, match="due_at"):
        create_planning_block(session, task_id=task.id, starts_at=at(17, 30), ends_at=at(18, 30))


def test_allow_split_can_be_updated_and_allows_multiple_non_overlapping_blocks(session):
    task = create_task(session, title="Aufteilbar")
    update_task_details(session, task_id=task.id, allow_split=True)
    create_planning_block(session, task_id=task.id, starts_at=at(10), ends_at=at(11))
    second = create_planning_block(session, task_id=task.id, starts_at=at(11), ends_at=at(12))

    assert len(list_planning_blocks(session, task_id=task.id)) == 2
    assert second.task_id == task.id


def test_scheduling_debug_api_uses_artificial_availability_and_block_storage(tmp_path):
    settings = make_settings(tmp_path, "api.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        task = client.post(
            "/tasks",
            json={
                "title": "API Planung",
                "allow_split": True,
                "earliest_start_at": "2026-09-21T10:00:00Z",
                "due_at": "2026-09-21T18:00:00Z",
                "estimated_minutes": 60,
            },
        ).json()
        availability = client.post(
            "/scheduling/availability/normalize",
            json={
                "windows": [
                    {"starts_at": "2026-09-21T14:00:00Z", "ends_at": "2026-09-21T16:00:00Z"},
                    {"starts_at": "2026-09-21T15:00:00Z", "ends_at": "2026-09-21T18:00:00Z"},
                ]
            },
        )
        block = client.post(
            "/planning-blocks",
            json={
                "task_id": task["id"],
                "starts_at": "2026-09-21T14:00:00Z",
                "ends_at": "2026-09-21T15:00:00Z",
            },
        )
        scheduling = client.get(f"/tasks/{task['id']}/scheduling")
        invalid_update = client.patch(f"/tasks/{task['id']}", json={"allow_split": None})

    assert availability.status_code == 200
    assert availability.json() == [
        {"starts_at": "2026-09-21T14:00:00Z", "ends_at": "2026-09-21T18:00:00Z"}
    ]
    assert block.status_code == 201
    assert scheduling.json()["allow_split"] is True
    assert scheduling.json()["planning_blocks"][0]["id"] == block.json()["id"]
    assert invalid_update.status_code == 422
