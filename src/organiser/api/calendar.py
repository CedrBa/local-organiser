"""HTTP calendar operations; authentication is inherited from the protected parent router."""

from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    CalendarSchedulingProposalCreate,
)
from organiser.services.calendar import CalendarAvailabilityProvider, availability_from_busy
from organiser.services.scheduling import AvailabilityWindow
from organiser.services.scheduler import (
    SchedulingProposalResult,
    apply_calendar_scheduling_proposal,
    create_scheduling_proposal,
)


def build_router(calendar_policy, configured_calendar_adapter, get_database_session) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.get("/calendar/calendars")
    def get_calendar_list():
        """Get calendar list through the protected GET /calendar/calendars endpoint."""
        return configured_calendar_adapter().list_calendars()

    @router.get("/calendar/busy")
    def get_calendar_busy(starts_at: datetime, ends_at: datetime):
        """Get calendar busy through the protected GET /calendar/busy endpoint."""
        return configured_calendar_adapter().busy_between(starts_at, ends_at)

    @router.get("/calendar/availability", response_model=list[AvailabilityWindow])
    def get_calendar_availability(starts_at: datetime, ends_at: datetime):
        """Get calendar availability through the protected GET /calendar/availability endpoint."""
        busy = configured_calendar_adapter().busy_between(starts_at, ends_at)
        return availability_from_busy(
            busy, starts_at=starts_at, ends_at=ends_at, policy=calendar_policy()
        )

    @router.post("/calendar/scheduling/proposals", response_model=SchedulingProposalResult)
    def create_calendar_schedule(
        request: CalendarSchedulingProposalCreate, session: Session = Depends(get_database_session)
    ):
        """Create calendar schedule through the protected POST /calendar/scheduling/proposals endpoint."""
        return create_scheduling_proposal(
            session,
            task_ids=request.task_ids,
            availability_provider=CalendarAvailabilityProvider(
                configured_calendar_adapter(), calendar_policy()
            ),
            horizon_starts_at=request.horizon_starts_at,
            horizon_ends_at=request.horizon_ends_at,
            policy=calendar_policy(),
        )

    @router.post("/calendar/organiser/setup")
    def setup_organiser_calendar():
        """Explicitly find or create the configured Organiser calendar.

        This is a remote write operation when the calendar is absent."""
        return configured_calendar_adapter().setup_organiser_calendar()

    @router.post(
        "/calendar/scheduling/proposals/{proposal_id}/apply",
        response_model=SchedulingProposalResult,
    )
    def apply_calendar_schedule(proposal_id: str, session: Session = Depends(get_database_session)):
        """Apply calendar schedule through the protected POST /calendar/scheduling/proposals/{proposal_id}/apply endpoint."""
        return apply_calendar_scheduling_proposal(
            session,
            proposal_id=proposal_id,
            adapter=configured_calendar_adapter(),
            policy=calendar_policy(),
        )

    return router
