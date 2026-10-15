"""Deterministic scheduling proposals built on Phase 5A availability and block primitives."""

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from organiser.db.models import PlanningBlock, SchedulingProposalRecord, Task
from organiser.services.domain import ConflictError, DomainValidationError, NotFoundError
from organiser.services.calendar import CalendarUnavailableError, OrganiserCalendarWriter
from organiser.services.scheduling import (
    AvailabilityProvider,
    AvailabilityWindow,
    DEFAULT_SCHEDULING_POLICY,
    SchedulingPolicy,
    SchedulingValidationError,
    create_planning_block,
)


class ScheduledBlockProposal(BaseModel):
    task_id: str
    starts_at: datetime
    ends_at: datetime
    minutes: int = Field(gt=0)


class ScheduledTaskProposal(BaseModel):
    task_id: str
    requested_minutes: int | None
    existing_minutes: int
    planned_minutes: int
    remaining_minutes: int
    blocks: list[ScheduledBlockProposal]
    warnings: list[str]


class SchedulingProposal(BaseModel):
    horizon_starts_at: datetime
    horizon_ends_at: datetime
    task_plans: list[ScheduledTaskProposal]
    blocks: list[ScheduledBlockProposal]
    warnings: list[str]
    policy: dict[str, int | bool | str]
    is_replan: bool


class SchedulingProposalResult(BaseModel):
    id: str
    status: str
    task_ids: list[str]
    availability: list[AvailabilityWindow]
    proposal: SchedulingProposal
    warnings: list[str]
    is_replan: bool
    error: str | None


def _stored_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _input_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SchedulingValidationError("Scheduling-Horizont braucht timezone-aware Zeitstempel")
    return value.astimezone(UTC)


def _minutes(starts_at: datetime, ends_at: datetime) -> int:
    return int((ends_at - starts_at).total_seconds() // 60)


def _reserve(
    windows: list[AvailabilityWindow], index: int, starts_at: datetime, ends_at: datetime
) -> None:
    window = windows.pop(index)
    replacements: list[AvailabilityWindow] = []
    if window.starts_at < starts_at:
        replacements.append(AvailabilityWindow(starts_at=window.starts_at, ends_at=starts_at))
    if ends_at < window.ends_at:
        replacements.append(AvailabilityWindow(starts_at=ends_at, ends_at=window.ends_at))
    windows[index:index] = replacements


def _subtract_busy(
    windows: list[AvailabilityWindow], blocks: list[PlanningBlock]
) -> list[AvailabilityWindow]:
    free = list(windows)
    for block in sorted(blocks, key=lambda value: (_stored_utc(value.starts_at), value.id)):
        starts_at, ends_at = _stored_utc(block.starts_at), _stored_utc(block.ends_at)
        remaining: list[AvailabilityWindow] = []
        for window in free:
            if window.starts_at < ends_at and window.ends_at > starts_at:
                if window.starts_at < starts_at:
                    remaining.append(
                        AvailabilityWindow(starts_at=window.starts_at, ends_at=starts_at)
                    )
                if ends_at < window.ends_at:
                    remaining.append(AvailabilityWindow(starts_at=ends_at, ends_at=window.ends_at))
            else:
                remaining.append(window)
        free = remaining
    return free


def _eligible_task(task: Task) -> bool:
    return task.status in {"open", "in_progress"}


def _priority(task: Task, horizon_starts_at: datetime) -> tuple[int, float, int, datetime, str]:
    load_order = {"high": 0, "medium": 1, "low": 2, "physical": 2}[task.cognitive_load]
    if task.due_at is None:
        return (1, float("inf"), load_order, _stored_utc(task.created_at), task.id)
    due_at = _stored_utc(task.due_at)
    earliest = (
        max(horizon_starts_at, _stored_utc(task.earliest_start_at))
        if task.earliest_start_at
        else horizon_starts_at
    )
    estimated_seconds = (task.estimated_minutes or 0) * 60
    return (
        0,
        (due_at - earliest).total_seconds() - estimated_seconds,
        load_order,
        _stored_utc(task.created_at),
        task.id,
    )


def _permitted_segments(
    window: AvailabilityWindow, *, task: Task, limit: datetime, policy: SchedulingPolicy
) -> list[tuple[datetime, datetime]]:
    timezone = ZoneInfo(policy.calendar_timezone)
    latest = min(window.ends_at, limit)
    if window.starts_at >= latest:
        return []
    local_start, local_end = window.starts_at.astimezone(timezone), latest.astimezone(timezone)
    day = local_start.date()
    segments: list[tuple[datetime, datetime]] = []
    while day <= local_end.date():
        allowed_end_hour = (
            policy.day_ends_hour
            if task.cognitive_load in {"low", "physical"}
            else policy.cognitive_ends_hour
        )
        allowed_start = (
            datetime.combine(day, datetime.min.time(), tzinfo=timezone)
            .replace(hour=policy.day_starts_hour)
            .astimezone(UTC)
        )
        allowed_end = (
            datetime.combine(day, datetime.min.time(), tzinfo=timezone)
            .replace(hour=allowed_end_hour)
            .astimezone(UTC)
        )
        starts_at, ends_at = max(window.starts_at, allowed_start), min(latest, allowed_end)
        if starts_at < ends_at:
            segments.append((starts_at, ends_at))
        day += timedelta(days=1)
    return segments


def _placement(
    starts_at: datetime, ends_at: datetime, minutes: int, task: Task
) -> tuple[datetime, datetime]:
    if task.cognitive_load in {"low", "physical"}:
        return ends_at - timedelta(minutes=minutes), ends_at
    return starts_at, starts_at + timedelta(minutes=minutes)


def _allocate(
    free: list[AvailabilityWindow],
    *,
    task: Task,
    remaining_minutes: int,
    limit: datetime,
    policy: SchedulingPolicy,
) -> list[ScheduledBlockProposal]:
    earliest = _stored_utc(task.earliest_start_at) if task.earliest_start_at else None
    if not task.allow_split:
        for index, window in enumerate(free):
            for starts_at, ends_at in _permitted_segments(
                window, task=task, limit=limit, policy=policy
            ):
                starts_at = max(starts_at, earliest) if earliest else starts_at
                if _minutes(starts_at, ends_at) >= remaining_minutes:
                    block_start, block_end = _placement(starts_at, ends_at, remaining_minutes, task)
                    _reserve(free, index, block_start, block_end)
                    return [
                        ScheduledBlockProposal(
                            task_id=task.id,
                            starts_at=block_start,
                            ends_at=block_end,
                            minutes=remaining_minutes,
                        )
                    ]
        return []

    # A single fitting interval is preferred over splitting, even for splittable tasks.
    for index, window in enumerate(free):
        for starts_at, ends_at in _permitted_segments(
            window, task=task, limit=limit, policy=policy
        ):
            starts_at = max(starts_at, earliest) if earliest else starts_at
            if _minutes(starts_at, ends_at) >= remaining_minutes:
                block_start, block_end = _placement(starts_at, ends_at, remaining_minutes, task)
                _reserve(free, index, block_start, block_end)
                return [
                    ScheduledBlockProposal(
                        task_id=task.id,
                        starts_at=block_start,
                        ends_at=block_end,
                        minutes=remaining_minutes,
                    )
                ]

    blocks: list[ScheduledBlockProposal] = []
    still_needed = remaining_minutes
    while still_needed >= policy.minimum_block_minutes:
        candidates = [
            (index, starts_at, ends_at)
            for index, window in enumerate(free)
            for starts_at, ends_at in _permitted_segments(
                window, task=task, limit=limit, policy=policy
            )
            if _minutes(max(starts_at, earliest) if earliest else starts_at, ends_at)
            >= policy.minimum_block_minutes
        ]
        if not candidates:
            break
        index, starts_at, ends_at = (
            candidates[-1] if task.cognitive_load in {"low", "physical"} else candidates[0]
        )
        starts_at = max(starts_at, earliest) if earliest else starts_at
        block_minutes = min(still_needed, _minutes(starts_at, ends_at))
        block_start, block_end = _placement(starts_at, ends_at, block_minutes, task)
        _reserve(free, index, block_start, block_end)
        blocks.append(
            ScheduledBlockProposal(
                task_id=task.id, starts_at=block_start, ends_at=block_end, minutes=block_minutes
            )
        )
        still_needed -= block_minutes
    return blocks


def _active_blocks(session: Session) -> list[PlanningBlock]:
    return list(
        session.scalars(
            select(PlanningBlock).where(PlanningBlock.status.in_(("planned", "completed")))
        )
    )


def _result(record: SchedulingProposalRecord) -> SchedulingProposalResult:
    return SchedulingProposalResult(
        id=record.id,
        status=record.status,
        task_ids=list(record.task_ids),
        availability=[
            AvailabilityWindow.model_validate(window) for window in record.availability_payload
        ],
        proposal=SchedulingProposal.model_validate(record.proposal_payload),
        warnings=list(record.warnings),
        is_replan=record.is_replan,
        error=record.error,
    )


def get_scheduling_proposal(session: Session, proposal_id: str) -> SchedulingProposalResult:
    """Read a scheduling proposal without creating or synchronising planning blocks."""
    record = session.get(SchedulingProposalRecord, proposal_id)
    if record is None:
        raise NotFoundError("Scheduling-Proposal nicht gefunden")
    return _result(record)


def create_scheduling_proposal(
    session: Session,
    *,
    task_ids: list[str],
    availability_provider: AvailabilityProvider,
    horizon_starts_at: datetime,
    horizon_ends_at: datetime,
    policy: SchedulingPolicy = DEFAULT_SCHEDULING_POLICY,
    is_replan: bool = False,
) -> SchedulingProposalResult:
    """Compute and persist deterministic block suggestions; leave blocks unchanged."""
    horizon_starts_at, horizon_ends_at = _input_utc(horizon_starts_at), _input_utc(horizon_ends_at)
    if horizon_ends_at <= horizon_starts_at:
        raise SchedulingValidationError("Scheduling-Horizont muss nach seinem Start enden")
    if not task_ids or len(task_ids) != len(set(task_ids)):
        raise SchedulingValidationError("task_ids muss eine nichtleere Liste eindeutiger IDs sein")

    tasks = [session.get(Task, task_id) for task_id in task_ids]
    if any(task is None for task in tasks):
        raise NotFoundError("Mindestens ein Task wurde nicht gefunden")
    typed_tasks = [task for task in tasks if task is not None]
    availability = availability_provider.availability_between(horizon_starts_at, horizon_ends_at)
    active_blocks = _active_blocks(session)
    free = _subtract_busy(availability, active_blocks)
    blocks_by_task: dict[str, list[PlanningBlock]] = {}
    for block in active_blocks:
        blocks_by_task.setdefault(block.task_id, []).append(block)

    task_plans: list[ScheduledTaskProposal] = []
    proposal_warnings: list[str] = []
    for task in sorted(typed_tasks, key=lambda value: _priority(value, horizon_starts_at)):
        warnings: list[str] = []
        existing_minutes = sum(
            _minutes(_stored_utc(block.starts_at), _stored_utc(block.ends_at))
            for block in blocks_by_task.get(task.id, [])
        )
        if not _eligible_task(task):
            warnings.append(f"task_status_{task.status}_not_scheduled")
            requested_minutes = task.estimated_minutes
            task_plans.append(
                ScheduledTaskProposal(
                    task_id=task.id,
                    requested_minutes=requested_minutes,
                    existing_minutes=existing_minutes,
                    planned_minutes=0,
                    remaining_minutes=max((requested_minutes or 0) - existing_minutes, 0),
                    blocks=[],
                    warnings=warnings,
                )
            )
            proposal_warnings.extend(warnings)
            continue
        if task.estimated_minutes is None:
            warnings.append("estimated_minutes_missing")
            task_plans.append(
                ScheduledTaskProposal(
                    task_id=task.id,
                    requested_minutes=None,
                    existing_minutes=existing_minutes,
                    planned_minutes=0,
                    remaining_minutes=0,
                    blocks=[],
                    warnings=warnings,
                )
            )
            proposal_warnings.extend(warnings)
            continue

        remaining = max(task.estimated_minutes - existing_minutes, 0)
        planned_blocks: list[ScheduledBlockProposal] = []
        if remaining and task.due_at is not None:
            due_at = _stored_utc(task.due_at)
            preferred_deadline = due_at - timedelta(minutes=policy.deadline_buffer_minutes)
            if preferred_deadline > horizon_starts_at:
                planned_blocks.extend(
                    _allocate(
                        free,
                        task=task,
                        remaining_minutes=remaining,
                        limit=preferred_deadline,
                        policy=policy,
                    )
                )
            remaining -= sum(block.minutes for block in planned_blocks)
            if remaining:
                fallback_blocks = _allocate(
                    free, task=task, remaining_minutes=remaining, limit=due_at, policy=policy
                )
                if fallback_blocks:
                    warnings.append("deadline_buffer_unmet")
                    planned_blocks.extend(fallback_blocks)
                    remaining -= sum(block.minutes for block in fallback_blocks)
        elif remaining:
            planned_blocks.extend(
                _allocate(
                    free,
                    task=task,
                    remaining_minutes=remaining,
                    limit=horizon_ends_at,
                    policy=policy,
                )
            )
            remaining -= sum(block.minutes for block in planned_blocks)
        if remaining:
            warnings.append(f"unscheduled_minutes:{remaining}")
        if is_replan and blocks_by_task.get(task.id):
            warnings.append("existing_blocks_preserved")
        task_plans.append(
            ScheduledTaskProposal(
                task_id=task.id,
                requested_minutes=task.estimated_minutes,
                existing_minutes=existing_minutes,
                planned_minutes=sum(block.minutes for block in planned_blocks),
                remaining_minutes=remaining,
                blocks=planned_blocks,
                warnings=warnings,
            )
        )
        proposal_warnings.extend(warnings)

    proposal = SchedulingProposal(
        horizon_starts_at=horizon_starts_at,
        horizon_ends_at=horizon_ends_at,
        task_plans=task_plans,
        blocks=[block for task_plan in task_plans for block in task_plan.blocks],
        warnings=proposal_warnings,
        policy=asdict(policy),
        is_replan=is_replan,
    )
    record = SchedulingProposalRecord(
        status="proposed",
        task_ids=task_ids,
        availability_payload=[window.model_dump(mode="json") for window in availability],
        policy_payload=asdict(policy),
        proposal_payload=proposal.model_dump(mode="json"),
        warnings=proposal_warnings,
        is_replan=is_replan,
    )
    session.add(record)
    session.commit()
    return _result(record)


def apply_scheduling_proposal(session: Session, *, proposal_id: str) -> SchedulingProposalResult:
    """Apply the current proposal once and create its internal blocks atomically."""
    record = session.get(SchedulingProposalRecord, proposal_id)
    if record is None:
        raise NotFoundError("Scheduling-Proposal nicht gefunden")
    if record.status != "proposed":
        raise ConflictError("Nur ein neues Scheduling-Proposal kann angewendet werden")
    try:
        proposal = SchedulingProposal.model_validate(record.proposal_payload)
        policy = SchedulingPolicy(**record.policy_payload)
        session.info["defer_scheduling_commit"] = True
        try:
            for block in proposal.blocks:
                create_planning_block(
                    session,
                    task_id=block.task_id,
                    starts_at=block.starts_at,
                    ends_at=block.ends_at,
                    source_plan_id=record.id,
                    policy=policy,
                )
        finally:
            session.info.pop("defer_scheduling_commit", None)
        record.status = "applied"
        record.applied_at = datetime.now(UTC)
        session.commit()
    except (DomainValidationError, SchedulingValidationError, ValueError) as error:
        session.rollback()
        record = session.get(SchedulingProposalRecord, proposal_id)
        if record is None:
            raise
        record.status = "failed"
        record.error = f"Scheduling-Proposal wurde nicht angewendet: {error}"
        session.commit()
    return _result(record)


def _calendar_conflict(adapter: OrganiserCalendarWriter, block: ScheduledBlockProposal) -> bool:
    return any(
        busy.starts_at < block.ends_at and busy.ends_at > block.starts_at
        for busy in adapter.busy_between(block.starts_at, block.ends_at)
    )


def _fail_calendar_apply(
    session: Session, record: SchedulingProposalRecord, blocks: list[PlanningBlock], error: str
) -> None:
    for block in blocks:
        if block.sync_status == "pending":
            block.status = "cancelled"
            block.sync_status = "failed"
            block.sync_error = error
    record.status = "failed"
    record.error = error
    session.commit()


def apply_calendar_scheduling_proposal(
    session: Session,
    *,
    proposal_id: str,
    adapter: OrganiserCalendarWriter,
    policy: SchedulingPolicy = DEFAULT_SCHEDULING_POLICY,
) -> SchedulingProposalResult:
    """Create only Organiser events after a fresh read-only conflict check for every block."""
    record = session.get(SchedulingProposalRecord, proposal_id)
    if record is None:
        raise NotFoundError("Scheduling-Proposal nicht gefunden")
    if record.status not in {"proposed", "syncing"}:
        raise ConflictError(
            "Scheduling-Proposal kann nicht mehr mit dem Organiser-Kalender synchronisiert werden"
        )
    proposal = SchedulingProposal.model_validate(record.proposal_payload)
    blocks = list(
        session.scalars(
            select(PlanningBlock)
            .where(PlanningBlock.source_plan_id == record.id)
            .order_by(PlanningBlock.starts_at)
        )
    )
    if record.status == "proposed":
        for proposal_block in proposal.blocks:
            if _calendar_conflict(adapter, proposal_block):
                record.status = "failed"
                record.error = "Calendar-Konflikt vor dem Organiser-Apply"
                session.commit()
                raise ConflictError(record.error)
        session.info["defer_scheduling_commit"] = True
        try:
            for proposal_block in proposal.blocks:
                block = create_planning_block(
                    session,
                    task_id=proposal_block.task_id,
                    starts_at=proposal_block.starts_at,
                    ends_at=proposal_block.ends_at,
                    source_plan_id=record.id,
                    policy=policy,
                )
                block.sync_status = "pending"
                blocks.append(block)
        finally:
            session.info.pop("defer_scheduling_commit", None)
        record.status = "syncing"
        session.commit()

    for block in blocks:
        if block.sync_status != "pending":
            continue
        proposed = ScheduledBlockProposal(
            task_id=block.task_id,
            starts_at=_stored_utc(block.starts_at),
            ends_at=_stored_utc(block.ends_at),
            minutes=_minutes(_stored_utc(block.starts_at), _stored_utc(block.ends_at)),
        )
        if _calendar_conflict(adapter, proposed):
            error = "Calendar-Konflikt vor dem Schreiben des Organiser-Events"
            _fail_calendar_apply(session, record, blocks, error)
            raise ConflictError(error)
        task = session.get(Task, block.task_id)
        if task is None:
            error = "Task fuer Organiser-Sync nicht gefunden"
            _fail_calendar_apply(session, record, blocks, error)
            raise NotFoundError(error)
        try:
            calendar_id, event_id = adapter.create_organiser_event(
                block_id=block.id,
                task_title=task.title,
                starts_at=proposed.starts_at,
                ends_at=proposed.ends_at,
            )
        except CalendarUnavailableError as error:
            _fail_calendar_apply(session, record, blocks, str(error))
            raise
        block.external_calendar_id = calendar_id
        block.external_event_id = event_id
        block.sync_status = "synced"
        block.synced_at = datetime.now(UTC)
        block.sync_error = None
        session.commit()

    record.status = "applied"
    record.applied_at = datetime.now(UTC)
    session.commit()
    return _result(record)
