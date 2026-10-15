"""FastAPI router factory for context-bound action proposals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from organiser.core.config import Settings
from organiser.domain.context_actions import ProposalValidationError
from organiser.services.context_actions import (
    apply_context_action_proposal,
    get_context_action_proposal,
    list_context_action_proposals,
    reject_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.context_knowledge import ContextKnowledgeAmbiguousError
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.local_llm import (
    ContextProposalClient,
    InterpreterClientError,
    OpenAICompatibleInterpreterClient,
)
from organiser.services.topic_knowledge import TopicKnowledgeAmbiguousError


class ContextActionProposalGenerateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_inbox_id: str | None = Field(default=None, max_length=64)
    resource_snapshot_id: str | None = Field(default=None, max_length=64)


class ContextActionProposalResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    context_id: str
    processing_run_id: str | None = None
    source_inbox_id: str | None = None
    resource_snapshot_id: str | None = None
    status: str
    action_payload: dict[str, Any]
    packet_payload: dict[str, Any]
    preconditions: dict[str, Any]
    model: str | None = None
    raw_output: str | None = None
    result_payload: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: datetime
    applied_at: datetime | None = None


def build_router(
    get_session: Callable[[], Session],
    get_store: Callable[[], KnowledgeStore],
    settings: Settings,
    client: ContextProposalClient | None = None,
) -> APIRouter:
    """Build the context-actions proposal/apply router."""
    router = APIRouter()

    def _get_client() -> ContextProposalClient:
        if client is not None:
            return client
        return OpenAICompatibleInterpreterClient(settings)

    @router.post(
        "/contexts/{context_id}/action-proposals",
        response_model=list[ContextActionProposalResponse],
        status_code=201,
    )
    def generate_proposals(
        context_id: str,
        request: ContextActionProposalGenerateRequest,
        session: Session = Depends(get_session),
        store: KnowledgeStore = Depends(get_store),
    ) -> list[ContextActionProposalResponse]:
        """Generate proposals through the protected POST /contexts/{context_id}/action-proposals endpoint."""
        try:
            proposals = propose_context_actions(
                session,
                context_id=context_id,
                store=store,
                settings=settings,
                client=_get_client(),
                source_inbox_id=request.source_inbox_id,
                resource_snapshot_id=request.resource_snapshot_id,
            )
            return [ContextActionProposalResponse.model_validate(p) for p in proposals]
        except NotFoundError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ConflictError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except (DomainValidationError, ProposalValidationError) as err:
            raise HTTPException(status_code=422, detail=str(err)) from err
        except InterpreterClientError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err

    @router.get(
        "/contexts/{context_id}/action-proposals",
        response_model=list[ContextActionProposalResponse],
    )
    def list_proposals(
        context_id: str,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        session: Session = Depends(get_session),
    ) -> list[ContextActionProposalResponse]:
        """List proposals through the protected GET /contexts/{context_id}/action-proposals endpoint."""
        try:
            proposals = list_context_action_proposals(
                session,
                context_id=context_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [ContextActionProposalResponse.model_validate(p) for p in proposals]
        except NotFoundError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.get(
        "/context-action-proposals/{proposal_id}",
        response_model=ContextActionProposalResponse,
    )
    def get_proposal(
        proposal_id: str,
        session: Session = Depends(get_session),
    ) -> ContextActionProposalResponse:
        """Get proposal through the protected GET /context-action-proposals/{proposal_id} endpoint."""
        try:
            proposal = get_context_action_proposal(session, proposal_id)
            return ContextActionProposalResponse.model_validate(proposal)
        except NotFoundError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err

    @router.post(
        "/context-action-proposals/{proposal_id}/apply",
        response_model=ContextActionProposalResponse,
    )
    def apply_proposal(
        proposal_id: str,
        session: Session = Depends(get_session),
        store: KnowledgeStore = Depends(get_store),
    ) -> ContextActionProposalResponse:
        """Apply proposal through the protected POST /context-action-proposals/{proposal_id}/apply endpoint."""
        try:
            proposal = apply_context_action_proposal(
                session,
                store=store,
                settings=settings,
                proposal_id=proposal_id,
            )
            return ContextActionProposalResponse.model_validate(proposal)
        except NotFoundError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except (ConflictError, ContextKnowledgeAmbiguousError, TopicKnowledgeAmbiguousError) as err:
            raise HTTPException(status_code=409, detail=str(err)) from err
        except DomainValidationError as err:
            raise HTTPException(status_code=422, detail=str(err)) from err

    @router.post(
        "/context-action-proposals/{proposal_id}/reject",
        response_model=ContextActionProposalResponse,
    )
    def reject_proposal(
        proposal_id: str,
        session: Session = Depends(get_session),
    ) -> ContextActionProposalResponse:
        """Reject proposal through the protected POST /context-action-proposals/{proposal_id}/reject endpoint."""
        try:
            proposal = reject_context_action_proposal(session, proposal_id)
            return ContextActionProposalResponse.model_validate(proposal)
        except NotFoundError as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        except ConflictError as err:
            raise HTTPException(status_code=409, detail=str(err)) from err

    return router
