from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import upgrade_database
from organiser.db.models import PlanningBlock
from organiser.db.session import create_session_factory
from organiser.services.domain import ConflictError, create_task
from organiser.services.scheduler import apply_scheduling_proposal, create_scheduling_proposal
from organiser.services.scheduling import (
    AvailabilityWindow,
    SchedulingPolicy,
    StaticAvailabilityProvider,
    create_planning_block,
    list_planning_blocks,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "scheduler.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 21, hour, minute, tzinfo=UTC)


def proposal(session, tasks, windows, *, replan=False, policy=SchedulingPolicy()):
    return create_scheduling_proposal(
        session,
        task_ids=[task.id for task in tasks],
        availability_provider=StaticAvailabilityProvider(
            [AvailabilityWindow(starts_at=start, ends_at=end) for start, end in windows]
        ),
        horizon_starts_at=at(8),
        horizon_ends_at=at(20),
        policy=policy,
        is_replan=replan,
    )


def task_plan(result, task_id):
    return next(plan for plan in result.proposal.task_plans if plan.task_id == task_id)


def test_60_minute_task_fits_single_slot_without_state_change(session):
    task = create_task(session, title="Kurz", estimated_minutes=60)
    result = proposal(session, [task], [(at(10), at(12))])

    plan = task_plan(result, task.id)
    assert [(block.starts_at, block.ends_at) for block in plan.blocks] == [(at(10), at(11))]
    assert session.scalars(select(PlanningBlock)).all() == []


def test_non_splittable_task_requires_one_large_enough_slot(session):
    task = create_task(session, title="Am Stueck", estimated_minutes=90, allow_split=False)
    result = proposal(session, [task], [(at(10), at(11)), (at(12), at(13))])

    plan = task_plan(result, task.id)
    assert plan.blocks == []
    assert plan.remaining_minutes == 90
    assert "unscheduled_minutes:90" in plan.warnings


def test_splittable_task_uses_multiple_slots_and_respects_minimum_block_size(session):
    task = create_task(session, title="Lang", estimated_minutes=170, allow_split=True)
    result = proposal(session, [task], [(at(10), at(11)), (at(12), at(14)), (at(15), at(15, 20))])

    plan = task_plan(result, task.id)
    assert [block.minutes for block in plan.blocks] == [60, 110]
    assert plan.remaining_minutes == 0
    assert all(block.minutes >= 30 for block in plan.blocks)


def test_earliest_start_and_real_deadline_are_never_violated(session):
    task = create_task(
        session, title="Begrenzt", estimated_minutes=60, earliest_start_at=at(10), due_at=at(13)
    )
    result = proposal(session, [task], [(at(8), at(11)), (at(12), at(14))])

    plan = task_plan(result, task.id)
    assert plan.blocks[0].starts_at >= at(10)
    assert plan.blocks[0].ends_at <= at(13)


def test_deadline_buffer_is_preferred_before_later_slots(session):
    task = create_task(session, title="Puffer", estimated_minutes=60, due_at=at(18))
    result = proposal(session, [task], [(at(10), at(11)), (at(17), at(18))])

    plan = task_plan(result, task.id)
    assert plan.blocks[0].starts_at == at(10)
    assert "deadline_buffer_unmet" not in plan.warnings


def test_deadline_buffer_fallback_warns_but_uses_real_deadline(session):
    task = create_task(session, title="Fallback", estimated_minutes=60, due_at=at(18))
    result = proposal(session, [task], [(at(17), at(18))])

    plan = task_plan(result, task.id)
    assert plan.blocks[0].ends_at == at(18)
    assert "deadline_buffer_unmet" in plan.warnings


def test_existing_blocks_are_subtracted_and_multiple_tasks_follow_priority(session):
    occupied = create_task(session, title="Belegt", estimated_minutes=60)
    create_planning_block(session, task_id=occupied.id, starts_at=at(10), ends_at=at(11))
    late = create_task(session, title="Spaeter", estimated_minutes=60, due_at=at(18))
    early = create_task(session, title="Frueher", estimated_minutes=60, due_at=at(14))
    result = proposal(session, [late, early], [(at(10), at(13))])

    assert task_plan(result, early.id).blocks[0].starts_at == at(11)
    assert task_plan(result, late.id).blocks[0].starts_at == at(12)
    all_blocks = result.proposal.blocks
    assert all(
        left.ends_at <= right.starts_at or right.ends_at <= left.starts_at
        for index, left in enumerate(all_blocks)
        for right in all_blocks[index + 1 :]
    )


def test_early_slots_avoid_unnecessary_deadline_crunch(session):
    task = create_task(session, title="Nicht warten", estimated_minutes=60, due_at=at(18))
    result = proposal(session, [task], [(at(9), at(10)), (at(17), at(18))])

    assert task_plan(result, task.id).blocks[0].starts_at == at(9)


def test_partial_capacity_reports_unscheduled_remainder(session):
    task = create_task(session, title="Zu lang", estimated_minutes=150, allow_split=True)
    result = proposal(session, [task], [(at(10), at(11)), (at(12), at(13))])

    plan = task_plan(result, task.id)
    assert plan.planned_minutes == 120
    assert plan.remaining_minutes == 30
    assert "unscheduled_minutes:30" in plan.warnings


def test_apply_creates_blocks_once_and_second_apply_is_blocked(session):
    task = create_task(session, title="Apply", estimated_minutes=60)
    proposed = proposal(session, [task], [(at(10), at(11))])

    applied = apply_scheduling_proposal(session, proposal_id=proposed.id)

    assert applied.status == "applied"
    blocks = list_planning_blocks(session, task_id=task.id)
    assert len(blocks) == 1
    assert blocks[0].source_plan_id == proposed.id
    with pytest.raises(ConflictError):
        apply_scheduling_proposal(session, proposal_id=proposed.id)
    assert len(list_planning_blocks(session, task_id=task.id)) == 1


def test_replanning_preserves_completed_and_future_blocks(session):
    task = create_task(session, title="Replan", estimated_minutes=180, allow_split=True)
    completed = PlanningBlock(
        task_id=task.id, starts_at=at(9), ends_at=at(10), status="completed", source_plan_id="old"
    )
    session.add(completed)
    session.commit()
    future = create_planning_block(
        session, task_id=task.id, starts_at=at(10), ends_at=at(11), source_plan_id="old"
    )

    proposed = proposal(session, [task], [(at(9), at(13))], replan=True)
    applied = apply_scheduling_proposal(session, proposal_id=proposed.id)

    blocks = list_planning_blocks(session, task_id=task.id)
    assert applied.status == "applied"
    assert session.get(PlanningBlock, completed.id).status == "completed"
    assert session.get(PlanningBlock, future.id).source_plan_id == "old"
    assert any(block.source_plan_id == proposed.id for block in blocks)
    assert "existing_blocks_preserved" in task_plan(proposed, task.id).warnings


def test_scheduling_api_creates_then_explicitly_applies_proposal(tmp_path):
    settings = make_settings(tmp_path, "scheduler-api.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        task = client.post("/tasks", json={"title": "API", "estimated_minutes": 60}).json()
        payload = {
            "task_ids": [task["id"]],
            "availability": [
                {"starts_at": "2026-09-21T10:00:00Z", "ends_at": "2026-09-21T11:00:00Z"}
            ],
            "horizon_starts_at": "2026-09-21T08:00:00Z",
            "horizon_ends_at": "2026-09-21T20:00:00Z",
        }
        proposed = client.post("/scheduling/proposals", json=payload)
        blocks_before = client.get(f"/planning-blocks?task_id={task['id']}")
        applied = client.post(f"/scheduling/proposals/{proposed.json()['id']}/apply")

    assert proposed.status_code == 200
    assert blocks_before.json() == []
    assert applied.json()["status"] == "applied"
