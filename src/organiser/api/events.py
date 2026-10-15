"""HTTP events operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    EventResponse,
)
from organiser.services.domain import (
    list_events_after_id,
    list_events_by_correlation_id,
)


def build_router(get_database_session) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.get("/events", response_model=list[EventResponse])
    def get_events(
        correlation_id: str | None = None,
        after_id: int | None = Query(default=None, ge=0),
        session: Session = Depends(get_database_session),
    ):
        """Get events through the protected GET /events endpoint."""
        if correlation_id is not None:
            return list_events_by_correlation_id(session, correlation_id)
        if after_id is not None:
            return list_events_after_id(session, after_id)
        raise HTTPException(status_code=422, detail="correlation_id oder after_id ist erforderlich")

    return router
