"""HTTP projects operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    EventContextInput,
    EventResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
    TaskResponse,
)
from organiser.domain.context import ContextPacket
from organiser.services.domain import (
    cancel_project,
    complete_project,
    create_project,
    get_project,
    list_events_for_entity,
    list_project_tasks,
    list_projects,
    pause_project,
    reopen_project,
    resume_project,
    update_project,
)
from organiser.services.context import project_context


def build_router(event_context, get_database_session) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/projects", response_model=ProjectResponse, status_code=201)
    def add_project(request: ProjectCreate, session: Session = Depends(get_database_session)):
        """Add project through the protected POST /projects endpoint."""
        return create_project(
            session,
            title=request.title,
            description=request.description,
            context=event_context(request.event_context),
        )

    @router.get("/projects", response_model=list[ProjectResponse])
    def get_projects(status: str | None = None, session: Session = Depends(get_database_session)):
        """Get projects through the protected GET /projects endpoint."""
        return list_projects(session, status=status)

    @router.get("/projects/{project_id}", response_model=ProjectResponse)
    def get_project_by_id(project_id: str, session: Session = Depends(get_database_session)):
        """Get project by id through the protected GET /projects/{project_id} endpoint."""
        return get_project(session, project_id)

    @router.patch("/projects/{project_id}", response_model=ProjectResponse)
    def patch_project(
        project_id: str, request: ProjectUpdate, session: Session = Depends(get_database_session)
    ):
        """Patch project through the protected PATCH /projects/{project_id} endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return update_project(
            session, project_id=project_id, context=event_context(request.event_context), **values
        )

    @router.post("/projects/{project_id}/pause", response_model=ProjectResponse)
    def pause_project_by_id(
        project_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Pause project by id through the protected POST /projects/{project_id}/pause endpoint."""
        return pause_project(session, project_id=project_id, context=event_context(context))

    @router.post("/projects/{project_id}/resume", response_model=ProjectResponse)
    def resume_project_by_id(
        project_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Resume project by id through the protected POST /projects/{project_id}/resume endpoint."""
        return resume_project(session, project_id=project_id, context=event_context(context))

    @router.post("/projects/{project_id}/complete", response_model=ProjectResponse)
    def complete_project_by_id(
        project_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Complete project by id through the protected POST /projects/{project_id}/complete endpoint."""
        return complete_project(session, project_id=project_id, context=event_context(context))

    @router.post("/projects/{project_id}/cancel", response_model=ProjectResponse)
    def cancel_project_by_id(
        project_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Cancel project by id through the protected POST /projects/{project_id}/cancel endpoint."""
        return cancel_project(session, project_id=project_id, context=event_context(context))

    @router.post("/projects/{project_id}/reopen", response_model=ProjectResponse)
    def reopen_project_by_id(
        project_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Reopen project by id through the protected POST /projects/{project_id}/reopen endpoint."""
        return reopen_project(session, project_id=project_id, context=event_context(context))

    @router.get("/projects/{project_id}/tasks", response_model=list[TaskResponse])
    def get_tasks_for_project(project_id: str, session: Session = Depends(get_database_session)):
        """Get tasks for project through the protected GET /projects/{project_id}/tasks endpoint."""
        return list_project_tasks(session, project_id)

    @router.get("/projects/{project_id}/events", response_model=list[EventResponse])
    def get_project_events(project_id: str, session: Session = Depends(get_database_session)):
        """Get project events through the protected GET /projects/{project_id}/events endpoint."""
        get_project(session, project_id)
        return list_events_for_entity(session, entity_type="project", entity_id=project_id)

    @router.get("/projects/{project_id}/context", response_model=ContextPacket)
    def get_project_context(project_id: str, session: Session = Depends(get_database_session)):
        """Get project context through the protected GET /projects/{project_id}/context endpoint."""
        return project_context(session, project_id)

    return router
