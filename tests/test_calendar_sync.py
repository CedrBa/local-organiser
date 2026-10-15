from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import upgrade_database
from organiser.db.models import PlanningBlock, SchedulingProposalRecord
from organiser.db.session import create_session_factory
from organiser.services.calendar import CalendarBusyInterval, CalendarInfo, CalendarUnavailableError
from organiser.services.domain import ConflictError, create_task
from organiser.services.scheduler import (
    apply_calendar_scheduling_proposal,
    create_scheduling_proposal,
)
from organiser.services.scheduling import AvailabilityWindow, StaticAvailabilityProvider


def at(hour: int) -> datetime:
    return datetime(2026, 9, 21, hour, tzinfo=UTC)


class FakeOrganiserCalendar:
    def __init__(self, *, busy=None, fail_writes=False):
        self.busy = busy or []
        self.fail_writes = fail_writes
        self.setup_calls = 0
        self.writes = []
        self.events = {}

    def list_calendars(self):
        return [CalendarInfo(name="Privat"), CalendarInfo(name="Uni")]

    def busy_between(self, starts_at, ends_at):
        return [item for item in self.busy if item.starts_at < ends_at and item.ends_at > starts_at]

    def setup_organiser_calendar(self):
        self.setup_calls += 1
        return CalendarInfo(name="Organiser")

    def create_organiser_event(self, *, block_id, task_title, starts_at, ends_at):
        if self.fail_writes:
            raise CalendarUnavailableError("CalDAV write failed")
        if block_id not in self.events:
            self.events[block_id] = f"https://calendar.test/Organiser/{block_id}.ics"
            self.writes.append((block_id, task_title, starts_at, ends_at))
        return "https://calendar.test/Organiser/", self.events[block_id]


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "calendar-sync.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def proposed(session, task):
    return create_scheduling_proposal(
        session,
        task_ids=[task.id],
        availability_provider=StaticAvailabilityProvider(
            [AvailabilityWindow(starts_at=at(10), ends_at=at(11))]
        ),
        horizon_starts_at=at(8),
        horizon_ends_at=at(20),
    )


def test_secret_setting_repr_does_not_expose_password(tmp_path):
    settings = make_settings(tmp_path, "config.sqlite3", caldav_app_password="")

    assert "caldav_app_password" not in repr(settings)


def test_free_block_creates_organiser_event_and_stores_external_reference(session):
    task = create_task(session, title="Hausarbeit", estimated_minutes=60)
    adapter = FakeOrganiserCalendar()
    result = apply_calendar_scheduling_proposal(
        session, proposal_id=proposed(session, task).id, adapter=adapter
    )

    block = session.scalar(select(PlanningBlock).where(PlanningBlock.task_id == task.id))
    assert result.status == "applied"
    assert adapter.writes == [(block.id, "Hausarbeit", at(10), at(11))]
    assert block.external_calendar_id == "https://calendar.test/Organiser/"
    assert block.external_event_id.endswith(f"{block.id}.ics")
    assert block.sync_status == "synced"


def test_second_calendar_apply_is_blocked_without_duplicate_event(session):
    task = create_task(session, title="Einmal", estimated_minutes=60)
    adapter = FakeOrganiserCalendar()
    result = proposed(session, task)
    apply_calendar_scheduling_proposal(session, proposal_id=result.id, adapter=adapter)

    with pytest.raises(ConflictError):
        apply_calendar_scheduling_proposal(session, proposal_id=result.id, adapter=adapter)
    assert len(adapter.writes) == 1


def test_new_private_or_uni_conflict_between_proposal_and_apply_blocks_write(session):
    task = create_task(session, title="Konflikt", estimated_minutes=60)
    result = proposed(session, task)
    adapter = FakeOrganiserCalendar(
        busy=[
            CalendarBusyInterval(
                calendar_name="Privat", external_event_id="new", starts_at=at(10), ends_at=at(11)
            )
        ]
    )

    with pytest.raises(ConflictError):
        apply_calendar_scheduling_proposal(session, proposal_id=result.id, adapter=adapter)
    assert adapter.writes == []
    assert (
        session.scalars(
            select(PlanningBlock).where(PlanningBlock.source_plan_id == result.id)
        ).all()
        == []
    )
    assert session.get(SchedulingProposalRecord, result.id).status == "failed"


def test_write_failure_cancels_unsynced_local_block_and_records_failure(session):
    task = create_task(session, title="Fehler", estimated_minutes=60)
    result = proposed(session, task)

    with pytest.raises(CalendarUnavailableError):
        apply_calendar_scheduling_proposal(
            session, proposal_id=result.id, adapter=FakeOrganiserCalendar(fail_writes=True)
        )

    block = session.scalar(select(PlanningBlock).where(PlanningBlock.source_plan_id == result.id))
    assert block.status == "cancelled"
    assert block.sync_status == "failed"
    assert "CalDAV write failed" in block.sync_error
    assert session.get(SchedulingProposalRecord, result.id).status == "failed"


def test_setup_is_explicit_and_never_part_of_apply(session):
    task = create_task(session, title="Setup", estimated_minutes=60)
    adapter = FakeOrganiserCalendar()

    apply_calendar_scheduling_proposal(
        session, proposal_id=proposed(session, task).id, adapter=adapter
    )

    assert adapter.setup_calls == 0
    assert adapter.setup_organiser_calendar().name == "Organiser"
    assert adapter.setup_calls == 1


def test_calendar_setup_and_apply_api_are_explicit(tmp_path):
    settings = make_settings(tmp_path, "calendar-sync-api.sqlite3")
    adapter = FakeOrganiserCalendar()
    with AuthenticatedTestClient(create_app(settings, calendar_adapter=adapter)) as client:
        assert client.post("/calendar/organiser/setup").json() == {"name": "Organiser"}
        task = client.post("/tasks", json={"title": "API Sync", "estimated_minutes": 60}).json()
        proposal_response = client.post(
            "/calendar/scheduling/proposals",
            json={
                "task_ids": [task["id"]],
                "horizon_starts_at": at(8).isoformat(),
                "horizon_ends_at": at(20).isoformat(),
            },
        )
        applied = client.post(
            f"/calendar/scheduling/proposals/{proposal_response.json()['id']}/apply"
        )

    assert proposal_response.status_code == 200
    assert applied.json()["status"] == "applied"
    assert len(adapter.writes) == 1
