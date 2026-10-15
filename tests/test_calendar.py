from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import upgrade_database
from organiser.db.session import create_session_factory
from organiser.services.calendar import (
    CalendarAvailabilityProvider,
    CalendarBusyInterval,
    CalendarInfo,
    CalendarUnavailableError,
    ICloudCalDAVAdapter,
    availability_from_busy,
)
from organiser.services.domain import create_task
from organiser.services.scheduler import create_scheduling_proposal
from organiser.services.scheduling import SchedulingPolicy, create_planning_block


BERLIN = ZoneInfo("Europe/Berlin")
POLICY = SchedulingPolicy(calendar_timezone="Europe/Berlin")


def local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, tzinfo=BERLIN)


class FakeCalendarAdapter:
    def __init__(self, busy: list[CalendarBusyInterval]):
        self.busy = busy

    def list_calendars(self):
        return [CalendarInfo(name="Privat"), CalendarInfo(name="Uni")]

    def busy_between(self, starts_at, ends_at):
        return [item for item in self.busy if item.starts_at < ends_at and item.ends_at > starts_at]


class FailingCalendarAdapter:
    def list_calendars(self):
        raise CalendarUnavailableError("iCloud nicht erreichbar")

    def busy_between(self, starts_at, ends_at):
        raise CalendarUnavailableError("iCloud nicht erreichbar")


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "calendar.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def calendar_proposal(session, tasks, adapter, *, starts_at=local(0), ends_at=local(23)):
    return create_scheduling_proposal(
        session,
        task_ids=[task.id for task in tasks],
        availability_provider=CalendarAvailabilityProvider(adapter, POLICY),
        horizon_starts_at=starts_at,
        horizon_ends_at=ends_at,
        policy=POLICY,
    )


def plan_for(result, task_id):
    return next(plan for plan in result.proposal.task_plans if plan.task_id == task_id)


@pytest.mark.parametrize("calendar_name", ["Privat", "Uni"])
def test_private_and_uni_events_block_availability(calendar_name):
    busy = CalendarBusyInterval(
        calendar_name=calendar_name,
        external_event_id="event-1",
        starts_at=local(10),
        ends_at=local(11),
        title="Fest",
    )
    availability = availability_from_busy(
        [busy], starts_at=local(9), ends_at=local(23), policy=POLICY
    )

    assert [
        (window.starts_at.astimezone(BERLIN).hour, window.ends_at.astimezone(BERLIN).hour)
        for window in availability
    ] == [(9, 10), (11, 23)]


def test_free_time_between_events_is_derived_correctly():
    busy = [
        CalendarBusyInterval(
            calendar_name="Privat", external_event_id="a", starts_at=local(10), ends_at=local(11)
        ),
        CalendarBusyInterval(
            calendar_name="Uni", external_event_id="b", starts_at=local(13), ends_at=local(14)
        ),
    ]
    availability = availability_from_busy(
        busy, starts_at=local(9), ends_at=local(16), policy=POLICY
    )

    assert [
        (window.starts_at.astimezone(BERLIN).hour, window.ends_at.astimezone(BERLIN).hour)
        for window in availability
    ] == [(9, 10), (11, 13), (14, 16)]


def test_calendar_availability_never_creates_time_outside_daily_framework(session):
    task = create_task(session, title="Normal", estimated_minutes=60)
    result = calendar_proposal(session, [task], FakeCalendarAdapter([]))
    block = plan_for(result, task.id).blocks[0]

    assert 9 <= block.starts_at.astimezone(BERLIN).hour < 21
    assert block.ends_at.astimezone(BERLIN).hour <= 21


def test_cognitive_task_is_not_scheduled_after_21_but_physical_task_is(session):
    normal = create_task(session, title="Hausarbeit", estimated_minutes=60, cognitive_load="medium")
    physical = create_task(session, title="Gym", estimated_minutes=60, cognitive_load="physical")
    busy = [
        CalendarBusyInterval(
            calendar_name="Privat", external_event_id="day", starts_at=local(9), ends_at=local(21)
        )
    ]
    adapter = FakeCalendarAdapter(busy)

    normal_result = calendar_proposal(session, [normal], adapter)
    physical_result = calendar_proposal(session, [physical], adapter)

    assert plan_for(normal_result, normal.id).blocks == []
    assert plan_for(physical_result, physical.id).blocks[0].starts_at.astimezone(BERLIN).hour >= 21


def test_high_cognitive_work_is_placed_before_physical_work_when_flexible(session):
    high = create_task(session, title="Mathe", estimated_minutes=60, cognitive_load="high")
    physical = create_task(
        session, title="Einkaufen", estimated_minutes=60, cognitive_load="physical"
    )
    result = calendar_proposal(session, [physical, high], FakeCalendarAdapter([]))

    high_block = plan_for(result, high.id).blocks[0]
    physical_block = plan_for(result, physical.id).blocks[0]
    assert high_block.ends_at <= physical_block.starts_at
    assert high_block.starts_at.astimezone(BERLIN).hour == 9
    assert physical_block.starts_at.astimezone(BERLIN).hour == 22


def test_deadline_is_stronger_than_time_of_day_preference(session):
    high = create_task(session, title="Flexibel", estimated_minutes=60, cognitive_load="high")
    physical = create_task(
        session, title="Dringend", estimated_minutes=60, cognitive_load="physical", due_at=local(10)
    )
    result = calendar_proposal(
        session, [high, physical], FakeCalendarAdapter([]), starts_at=local(9), ends_at=local(12)
    )

    assert plan_for(result, physical.id).blocks[0].ends_at == local(10).astimezone(UTC)
    assert plan_for(result, high.id).blocks[0].starts_at >= local(10).astimezone(UTC)


def test_existing_planning_blocks_remain_busy_for_calendar_scheduling(session):
    occupied = create_task(session, title="Bestehend", estimated_minutes=60)
    create_planning_block(session, task_id=occupied.id, starts_at=local(9), ends_at=local(10))
    task = create_task(session, title="Neu", estimated_minutes=60)
    result = calendar_proposal(session, [task], FakeCalendarAdapter([]))

    assert plan_for(result, task.id).blocks[0].starts_at == local(10).astimezone(UTC)


def test_calendar_failure_does_not_produce_free_time_or_proposal(session):
    task = create_task(session, title="Sicher", estimated_minutes=60)
    with pytest.raises(CalendarUnavailableError):
        calendar_proposal(session, [task], FailingCalendarAdapter())


def test_calendar_debug_and_proposal_api_use_read_only_fake_adapter(tmp_path):
    settings = make_settings(tmp_path, "calendar-api.sqlite3")
    busy = [
        CalendarBusyInterval(
            calendar_name="Uni",
            external_event_id="uni-1",
            starts_at=local(10),
            ends_at=local(11),
            title="Vorlesung",
        )
    ]
    with AuthenticatedTestClient(
        create_app(settings, calendar_adapter=FakeCalendarAdapter(busy))
    ) as client:
        calendars = client.get("/calendar/calendars")
        busy_response = client.get(
            "/calendar/busy",
            params={"starts_at": local(9).isoformat(), "ends_at": local(12).isoformat()},
        )
        availability = client.get(
            "/calendar/availability",
            params={"starts_at": local(9).isoformat(), "ends_at": local(12).isoformat()},
        )
        task = client.post("/tasks", json={"title": "API", "estimated_minutes": 60}).json()
        proposed = client.post(
            "/calendar/scheduling/proposals",
            json={
                "task_ids": [task["id"]],
                "horizon_starts_at": local(9).isoformat(),
                "horizon_ends_at": local(12).isoformat(),
            },
        )

    assert [item["name"] for item in calendars.json()] == ["Privat", "Uni"]
    assert busy_response.json()[0]["external_event_id"] == "uni-1"
    assert len(availability.json()) == 2
    assert proposed.status_code == 200


def test_calendar_unavailable_api_returns_503_without_scheduling(tmp_path):
    settings = make_settings(tmp_path, "calendar-error.sqlite3")
    with AuthenticatedTestClient(
        create_app(settings, calendar_adapter=FailingCalendarAdapter())
    ) as client:
        response = client.get(
            "/calendar/availability",
            params={"starts_at": local(9).isoformat(), "ends_at": local(12).isoformat()},
        )

    assert response.status_code == 503


def test_caldav_adapter_uses_only_calendar_read_methods():
    component = SimpleNamespace(
        dtstart=SimpleNamespace(value=local(10)),
        dtend=SimpleNamespace(value=local(11)),
        uid=SimpleNamespace(value="uid-1"),
        summary=SimpleNamespace(value="Vorlesung"),
    )
    event = SimpleNamespace(
        vobject_instance=SimpleNamespace(vevent=component), url="https://example.test/event-1"
    )
    calendar = SimpleNamespace(name="Privat", date_search=lambda **kwargs: [event])
    uni_calendar = SimpleNamespace(name="Uni", date_search=lambda **kwargs: [])
    client = SimpleNamespace(
        principal=lambda: SimpleNamespace(calendars=lambda: [calendar, uni_calendar])
    )
    adapter = ICloudCalDAVAdapter(
        url="https://example.test",
        username="",
        app_password="",
        calendar_names=("Privat", "Uni"),
        organiser_calendar="Organiser",
        timezone="Europe/Berlin",
        client_factory=lambda *_: client,
    )

    assert [item.name for item in adapter.list_calendars()] == ["Privat", "Uni"]
    assert adapter.busy_between(local(9), local(12))[0].title == "Vorlesung"
