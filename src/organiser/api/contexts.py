"""HTTP contexts operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    EventResponse,
    ContextCreate,
    ContextResponse,
    ContextSourceCreate,
    ContextSourceResponse,
    ContextSourceUpdate,
    ContextStatusUpdate,
    ContextUpdate,
)
from organiser.services.domain import (
    add_context_source,
    create_context,
    get_context,
    get_context_source,
    list_child_contexts,
    list_context_sources,
    list_contexts,
    list_events_for_entity,
    remove_context_source,
    set_context_status,
    update_context,
    update_context_source,
)


def build_router(event_context, get_database_session) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/contexts", response_model=ContextResponse, status_code=201)
    def add_context(request: ContextCreate, session: Session = Depends(get_database_session)):
        """Add context through the protected POST /contexts endpoint."""
        values = request.model_dump(exclude={"event_context"})
        return create_context(session, context=event_context(request.event_context), **values)

    @router.get("/contexts", response_model=list[ContextResponse])
    def get_contexts(
        status: str | None = None,
        type: str | None = None,
        parent_context_id: str | None = None,
        roots_only: bool = False,
        session: Session = Depends(get_database_session),
    ):
        """Get contexts through the protected GET /contexts endpoint."""
        return list_contexts(
            session,
            status=status,
            type=type,
            parent_context_id=parent_context_id,
            roots_only=roots_only,
        )

    @router.get("/contexts/{context_id}", response_model=ContextResponse)
    def get_context_by_id(context_id: str, session: Session = Depends(get_database_session)):
        """Get context by id through the protected GET /contexts/{context_id} endpoint."""
        return get_context(session, context_id)

    @router.patch("/contexts/{context_id}", response_model=ContextResponse)
    def patch_context(
        context_id: str, request: ContextUpdate, session: Session = Depends(get_database_session)
    ):
        """Patch context through the protected PATCH /contexts/{context_id} endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return update_context(
            session, context_id=context_id, context=event_context(request.event_context), **values
        )

    @router.post("/contexts/{context_id}/status", response_model=ContextResponse)
    def patch_context_status(
        context_id: str,
        request: ContextStatusUpdate,
        session: Session = Depends(get_database_session),
    ):
        """Patch context status through the protected POST /contexts/{context_id}/status endpoint."""
        return set_context_status(
            session,
            context_id=context_id,
            status=request.status,
            context=event_context(request.event_context),
        )

    @router.get("/contexts/{context_id}/children", response_model=list[ContextResponse])
    def get_context_children(context_id: str, session: Session = Depends(get_database_session)):
        """Get context children through the protected GET /contexts/{context_id}/children endpoint."""
        return list_child_contexts(session, context_id)

    @router.get("/contexts/{context_id}/events", response_model=list[EventResponse])
    def get_context_events(context_id: str, session: Session = Depends(get_database_session)):
        """Get context events through the protected GET /contexts/{context_id}/events endpoint."""
        get_context(session, context_id)
        return list_events_for_entity(session, entity_type="context", entity_id=context_id)

    @router.post(
        "/contexts/{context_id}/sources", response_model=ContextSourceResponse, status_code=201
    )
    def add_source_for_context(
        context_id: str,
        request: ContextSourceCreate,
        session: Session = Depends(get_database_session),
    ):
        """Add source for context through the protected POST /contexts/{context_id}/sources endpoint."""
        values = request.model_dump(exclude={"event_context"})
        return add_context_source(
            session, context_id=context_id, context=event_context(request.event_context), **values
        )

    @router.get("/contexts/{context_id}/sources", response_model=list[ContextSourceResponse])
    def get_context_sources(
        context_id: str, status: str | None = None, session: Session = Depends(get_database_session)
    ):
        """Get context sources through the protected GET /contexts/{context_id}/sources endpoint."""
        return list_context_sources(session, context_id=context_id, status=status)

    @router.get("/contexts/{context_id}/sources/{source_id}", response_model=ContextSourceResponse)
    def get_source_by_id(
        context_id: str, source_id: str, session: Session = Depends(get_database_session)
    ):
        """Get source by id through the protected GET /contexts/{context_id}/sources/{source_id} endpoint."""
        return get_context_source(session, context_id=context_id, source_id=source_id)

    @router.patch(
        "/contexts/{context_id}/sources/{source_id}", response_model=ContextSourceResponse
    )
    def patch_source(
        context_id: str,
        source_id: str,
        request: ContextSourceUpdate,
        session: Session = Depends(get_database_session),
    ):
        """Patch source through the protected PATCH /contexts/{context_id}/sources/{source_id} endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return update_context_source(
            session,
            context_id=context_id,
            source_id=source_id,
            context=event_context(request.event_context),
            **values,
        )

    @router.delete("/contexts/{context_id}/sources/{source_id}", status_code=204)
    def delete_source(
        context_id: str, source_id: str, session: Session = Depends(get_database_session)
    ):
        """Delete source through the protected DELETE /contexts/{context_id}/sources/{source_id} endpoint."""
        remove_context_source(session, context_id=context_id, source_id=source_id)
        return Response(status_code=204)

    return router
