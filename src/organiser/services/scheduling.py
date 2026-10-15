"""Deterministic scheduling primitives; no scheduler or calendar integration runs here."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Iterable, Protocol

from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from organiser.db.models import PlanningBlock, Task
from organiser.services.domain import DomainValidationError, get_task


class SchedulingValidationError(DomainValidationError):
    pass


@dataclass(frozen=True)
class SchedulingPolicy:
    minimum_block_minutes: int = 30
    deadline_buffer_minutes: int = 60
    prevent_overlaps: bool = True
    respect_earliest_start: bool = True
    respect_due_at: bool = True
    require_explicit_task_splitting: bool = True
    preserve_contiguous_availability: bool = True
    calendar_timezone: str = "Europe/Berlin"
    day_starts_hour: int = 9
    day_ends_hour: int = 23
    cognitive_ends_hour: int = 21


DEFAULT_SCHEDULING_POLICY = SchedulingPolicy()


class AvailabilityWindow(BaseModel):
    starts_at: datetime
    ends_at: datetime

    @field_validator("starts_at", "ends_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        """Require timezone-aware input and normalise it to UTC."""
        if value.tzinfo is None:
            raise ValueError("Availability-Zeitstempel muessen eine Zeitzone enthalten")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_interval(self):
        """Validate an aware, strictly increasing time interval before scheduling."""
        if self.ends_at <= self.starts_at:
            raise ValueError("Availability-Fenster muss nach seinem Start enden")
        return self


class AvailabilityProvider(Protocol):
    def availability_between(
        self, starts_at: datetime, ends_at: datetime
    ) -> list[AvailabilityWindow]:
        """Return availability within the requested interval using this provider contract."""
        ...


def normalize_availability(windows: Iterable[AvailabilityWindow]) -> list[AvailabilityWindow]:
    """Sort and merge touching windows so schedulers receive one deterministic free-time view."""
    normalized = sorted(windows, key=lambda window: (window.starts_at, window.ends_at))
    merged: list[AvailabilityWindow] = []
    for window in normalized:
        if merged and window.starts_at <= merged[-1].ends_at:
            merged[-1] = AvailabilityWindow(
                starts_at=merged[-1].starts_at, ends_at=max(merged[-1].ends_at, window.ends_at)
            )
        else:
            merged.append(window)
    return merged


class StaticAvailabilityProvider:
    """In-memory availability for APIs and tests; calendar adapters can implement the same protocol later."""

    def __init__(self, windows: Iterable[AvailabilityWindow]):
        self._windows = normalize_availability(windows)

    def availability_between(
        self, starts_at: datetime, ends_at: datetime
    ) -> list[AvailabilityWindow]:
        """Return availability within the requested interval using this provider contract."""
        query = AvailabilityWindow(starts_at=starts_at, ends_at=ends_at)
        return [
            AvailabilityWindow(
                starts_at=max(window.starts_at, query.starts_at),
                ends_at=min(window.ends_at, query.ends_at),
            )
            for window in self._windows
            if window.starts_at < query.ends_at and window.ends_at > query.starts_at
        ]


class PlanningBlockView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    task_id: str
    starts_at: datetime
    ends_at: datetime
    status: str
    source_plan_id: str | None
    external_calendar_id: str | None
    external_event_id: str | None
    sync_status: str
    synced_at: datetime | None
    sync_error: str | None
    created_at: datetime
    updated_at: datetime

    @field_validator("starts_at", "ends_at", "created_at", "updated_at")
    @classmethod
    def normalize_persisted_time(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class TaskSchedulingInfo(BaseModel):
    id: str
    earliest_start_at: datetime | None
    due_at: datetime | None
    estimated_minutes: int | None
    allow_split: bool
    cognitive_load: str
    planning_blocks: list[PlanningBlockView]


def _normalized_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise SchedulingValidationError("Planungszeitstempel muessen eine Zeitzone enthalten")
    return value.astimezone(UTC)


def _stored_time(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def list_planning_blocks(session: Session, *, task_id: str | None = None) -> list[PlanningBlock]:
    """Read existing internal blocks, optionally restricted to one task."""
    statement = select(PlanningBlock).order_by(PlanningBlock.starts_at, PlanningBlock.id)
    if task_id is not None:
        get_task(session, task_id)
        statement = statement.where(PlanningBlock.task_id == task_id)
    return list(session.scalars(statement))


def create_planning_block(
    session: Session,
    *,
    task_id: str,
    starts_at: datetime,
    ends_at: datetime,
    source_plan_id: str | None = None,
    policy: SchedulingPolicy = DEFAULT_SCHEDULING_POLICY,
) -> PlanningBlock:
    """Validate policy and overlap rules, then persist an internal planning block."""
    task = get_task(session, task_id)
    starts_at, ends_at = _normalized_time(starts_at), _normalized_time(ends_at)
    if ends_at <= starts_at:
        raise SchedulingValidationError("Planning Block muss nach seinem Start enden")
    if ends_at - starts_at < timedelta(minutes=policy.minimum_block_minutes):
        raise SchedulingValidationError("Planning Block unterschreitet die Mindestgroesse")
    if (
        policy.respect_earliest_start
        and task.earliest_start_at is not None
        and starts_at < _stored_time(task.earliest_start_at)
    ):
        raise SchedulingValidationError("Planning Block liegt vor earliest_start_at")
    if policy.respect_due_at and task.due_at is not None and ends_at > _stored_time(task.due_at):
        raise SchedulingValidationError("Planning Block endet nach due_at")

    active_blocks = list(
        session.scalars(
            select(PlanningBlock).where(
                PlanningBlock.status == "planned", PlanningBlock.task_id == task.id
            )
        )
    )
    if policy.require_explicit_task_splitting and active_blocks and not task.allow_split:
        raise SchedulingValidationError("Task erlaubt keine Aufteilung auf mehrere Planning Blocks")
    if policy.prevent_overlaps and session.scalar(
        select(PlanningBlock.id)
        .where(
            PlanningBlock.status == "planned",
            PlanningBlock.starts_at < ends_at,
            PlanningBlock.ends_at > starts_at,
        )
        .limit(1)
    ):
        raise SchedulingValidationError(
            "Planning Block ueberschneidet einen bestehenden geplanten Block"
        )

    block = PlanningBlock(
        task_id=task.id,
        starts_at=starts_at,
        ends_at=ends_at,
        status="planned",
        source_plan_id=source_plan_id,
    )
    session.add(block)
    if session.info.get("defer_scheduling_commit"):
        session.flush()
    else:
        session.commit()
    return block


def task_scheduling_info(session: Session, task_id: str) -> TaskSchedulingInfo:
    """Read the task scheduling fields and associated blocks without replanning."""
    task: Task = get_task(session, task_id)
    return TaskSchedulingInfo(
        id=task.id,
        earliest_start_at=_stored_time(task.earliest_start_at) if task.earliest_start_at else None,
        due_at=_stored_time(task.due_at) if task.due_at else None,
        estimated_minutes=task.estimated_minutes,
        allow_split=task.allow_split,
        cognitive_load=task.cognitive_load,
        planning_blocks=[
            PlanningBlockView.model_validate(block)
            for block in list_planning_blocks(session, task_id=task.id)
        ],
    )
