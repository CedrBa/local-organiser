"""HTTP planning operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from organiser.domain.proposals import PlanningProposalResult
from organiser.services.local_llm import OpenAICompatibleInterpreterClient
from organiser.services.planner import (
    apply_planning_proposal,
    create_planning_proposal,
    get_planning_proposal,
)


def build_router(app_settings, get_database_session, get_store, llm_client) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/tasks/{task_id}/plan", response_model=PlanningProposalResult)
    def plan_task(task_id: str, session: Session = Depends(get_database_session)):
        """Plan task through the protected POST /tasks/{task_id}/plan endpoint."""
        client = llm_client or OpenAICompatibleInterpreterClient(app_settings)
        return create_planning_proposal(
            session, target_type="task", target_id=task_id, client=client, store=get_store()
        )

    @router.post("/projects/{project_id}/plan", response_model=PlanningProposalResult)
    def plan_project(project_id: str, session: Session = Depends(get_database_session)):
        """Plan project through the protected POST /projects/{project_id}/plan endpoint."""
        client = llm_client or OpenAICompatibleInterpreterClient(app_settings)
        return create_planning_proposal(
            session, target_type="project", target_id=project_id, client=client, store=get_store()
        )

    @router.get("/planning/proposals/{proposal_id}", response_model=PlanningProposalResult)
    def get_plan(proposal_id: str, session: Session = Depends(get_database_session)):
        """Get plan through the protected GET /planning/proposals/{proposal_id} endpoint."""
        return get_planning_proposal(session, proposal_id)

    @router.post("/planning/proposals/{proposal_id}/apply", response_model=PlanningProposalResult)
    def apply_plan(proposal_id: str, session: Session = Depends(get_database_session)):
        """Apply plan through the protected POST /planning/proposals/{proposal_id}/apply endpoint."""
        return apply_planning_proposal(session, proposal_id=proposal_id)

    return router
