"""HTTP scheduling operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    AvailabilityNormalizeRequest,
    SchedulingProposalCreate,
    PlanningBlockCreate,
    PlanningBlockResponse,
)
from organiser.services.scheduling import (
    AvailabilityWindow,
    StaticAvailabilityProvider,
    create_planning_block,
    list_planning_blocks,
    normalize_availability,
)
from organiser.services.scheduler import (
    SchedulingProposalResult,
    apply_scheduling_proposal,
    create_scheduling_proposal,
    get_scheduling_proposal,
)


def build_router(get_database_session) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/scheduling/availability/normalize", response_model=list[AvailabilityWindow])
    def normalize_scheduling_availability(request: AvailabilityNormalizeRequest):
        """Normalize scheduling availability through the protected POST /scheduling/availability/normalize endpoint."""
        return normalize_availability(
            [AvailabilityWindow(**window.model_dump()) for window in request.windows]
        )

    @router.post("/scheduling/proposals", response_model=SchedulingProposalResult)
    def create_schedule(
        request: SchedulingProposalCreate, session: Session = Depends(get_database_session)
    ):
        """Create schedule through the protected POST /scheduling/proposals endpoint."""
        provider = StaticAvailabilityProvider(
            [AvailabilityWindow(**window.model_dump()) for window in request.availability]
        )
        return create_scheduling_proposal(
            session,
            task_ids=request.task_ids,
            availability_provider=provider,
            horizon_starts_at=request.horizon_starts_at,
            horizon_ends_at=request.horizon_ends_at,
        )

    @router.post("/scheduling/replan", response_model=SchedulingProposalResult)
    def replan_schedule(
        request: SchedulingProposalCreate, session: Session = Depends(get_database_session)
    ):
        """Replan schedule through the protected POST /scheduling/replan endpoint."""
        provider = StaticAvailabilityProvider(
            [AvailabilityWindow(**window.model_dump()) for window in request.availability]
        )
        return create_scheduling_proposal(
            session,
            task_ids=request.task_ids,
            availability_provider=provider,
            horizon_starts_at=request.horizon_starts_at,
            horizon_ends_at=request.horizon_ends_at,
            is_replan=True,
        )

    @router.get("/scheduling/proposals/{proposal_id}", response_model=SchedulingProposalResult)
    def get_schedule(proposal_id: str, session: Session = Depends(get_database_session)):
        """Get schedule through the protected GET /scheduling/proposals/{proposal_id} endpoint."""
        return get_scheduling_proposal(session, proposal_id)

    @router.post(
        "/scheduling/proposals/{proposal_id}/apply", response_model=SchedulingProposalResult
    )
    def apply_schedule(proposal_id: str, session: Session = Depends(get_database_session)):
        """Apply schedule through the protected POST /scheduling/proposals/{proposal_id}/apply endpoint."""
        return apply_scheduling_proposal(session, proposal_id=proposal_id)

    @router.post("/planning-blocks", response_model=PlanningBlockResponse, status_code=201)
    def add_planning_block(
        request: PlanningBlockCreate, session: Session = Depends(get_database_session)
    ):
        """Add planning block through the protected POST /planning-blocks endpoint."""
        return create_planning_block(session, **request.model_dump())

    @router.get("/planning-blocks", response_model=list[PlanningBlockResponse])
    def get_planning_blocks(
        task_id: str | None = None, session: Session = Depends(get_database_session)
    ):
        """Get planning blocks through the protected GET /planning-blocks endpoint."""
        return list_planning_blocks(session, task_id=task_id)

    return router
