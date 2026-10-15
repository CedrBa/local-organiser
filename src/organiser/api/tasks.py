"""HTTP tasks operations; authentication is inherited from the protected parent router."""

from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from organiser.api.schemas import (
    EventContextInput,
    EventResponse,
    TaskCreate,
    TaskReschedule,
    TaskResponse,
    TaskSchedulingResponse,
    TaskUpdate,
)
from organiser.domain.context import ContextPacket
from organiser.services.domain import (
    cancel_task,
    complete_task,
    create_task,
    get_task,
    list_child_tasks,
    list_events_for_entity,
    list_tasks,
    mark_task_waiting,
    reopen_task,
    reschedule_task,
    start_task,
    update_task_details,
)
from organiser.services.context import task_context
from organiser.services.scheduling import task_scheduling_info


def build_router(event_context, get_database_session, get_store) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/tasks", response_model=TaskResponse, status_code=201)
    def add_task(request: TaskCreate, session: Session = Depends(get_database_session)):
        # exclude_unset: fehlendes context_id/topic_id (erbt vom Parent) ist etwas anderes als explizites null.
        """Add task through the protected POST /tasks endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return create_task(session, context=event_context(request.event_context), **values)

    @router.get("/tasks", response_model=list[TaskResponse])
    def get_tasks(
        status: str | None = None,
        project_id: str | None = None,
        has_deadline: bool | None = None,
        due_from: datetime | None = None,
        due_to: datetime | None = None,
        parent_task_id: str | None = None,
        context_id: str | None = None,
        topic_id: str | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Get tasks through the protected GET /tasks endpoint."""
        return list_tasks(
            session,
            status=status,
            project_id=project_id,
            has_deadline=has_deadline,
            due_from=due_from,
            due_to=due_to,
            parent_task_id=parent_task_id,
            context_id=context_id,
            topic_id=topic_id,
        )

    @router.get("/tasks/{task_id}", response_model=TaskResponse)
    def get_task_by_id(task_id: str, session: Session = Depends(get_database_session)):
        """Get task by id through the protected GET /tasks/{task_id} endpoint."""
        return get_task(session, task_id)

    @router.patch("/tasks/{task_id}", response_model=TaskResponse)
    def patch_task(
        task_id: str, request: TaskUpdate, session: Session = Depends(get_database_session)
    ):
        """Patch task through the protected PATCH /tasks/{task_id} endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return update_task_details(
            session, task_id=task_id, context=event_context(request.event_context), **values
        )

    @router.post("/tasks/{task_id}/start", response_model=TaskResponse)
    def start_task_by_id(
        task_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Start task by id through the protected POST /tasks/{task_id}/start endpoint."""
        return start_task(session, task_id=task_id, context=event_context(context))

    @router.post("/tasks/{task_id}/waiting", response_model=TaskResponse)
    def wait_task_by_id(
        task_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Wait task by id through the protected POST /tasks/{task_id}/waiting endpoint."""
        return mark_task_waiting(session, task_id=task_id, context=event_context(context))

    @router.post("/tasks/{task_id}/complete", response_model=TaskResponse)
    def complete_task_by_id(
        task_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Complete task by id through the protected POST /tasks/{task_id}/complete endpoint."""
        return complete_task(session, task_id=task_id, context=event_context(context))

    @router.post("/tasks/{task_id}/reopen", response_model=TaskResponse)
    def reopen_task_by_id(
        task_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Reopen task by id through the protected POST /tasks/{task_id}/reopen endpoint."""
        return reopen_task(session, task_id=task_id, context=event_context(context))

    @router.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
    def cancel_task_by_id(
        task_id: str,
        context: EventContextInput | None = None,
        session: Session = Depends(get_database_session),
    ):
        """Cancel task by id through the protected POST /tasks/{task_id}/cancel endpoint."""
        return cancel_task(session, task_id=task_id, context=event_context(context))

    @router.post("/tasks/{task_id}/reschedule", response_model=TaskResponse)
    def reschedule_task_by_id(
        task_id: str, request: TaskReschedule, session: Session = Depends(get_database_session)
    ):
        """Reschedule task by id through the protected POST /tasks/{task_id}/reschedule endpoint."""
        return reschedule_task(
            session,
            task_id=task_id,
            earliest_start_at=request.earliest_start_at,
            due_at=request.due_at,
            context=event_context(request.event_context),
        )

    @router.get("/tasks/{task_id}/children", response_model=list[TaskResponse])
    def get_task_children(task_id: str, session: Session = Depends(get_database_session)):
        """Get task children through the protected GET /tasks/{task_id}/children endpoint."""
        return list_child_tasks(session, task_id)

    @router.get("/tasks/{task_id}/events", response_model=list[EventResponse])
    def get_task_events(task_id: str, session: Session = Depends(get_database_session)):
        """Get task events through the protected GET /tasks/{task_id}/events endpoint."""
        get_task(session, task_id)
        return list_events_for_entity(session, entity_type="task", entity_id=task_id)

    @router.get("/tasks/{task_id}/context", response_model=ContextPacket)
    def get_task_context(task_id: str, session: Session = Depends(get_database_session)):
        """Get task context through the protected GET /tasks/{task_id}/context endpoint."""
        return task_context(session, task_id, store=get_store())

    @router.get("/tasks/{task_id}/scheduling", response_model=TaskSchedulingResponse)
    def get_task_scheduling(task_id: str, session: Session = Depends(get_database_session)):
        """Get task scheduling through the protected GET /tasks/{task_id}/scheduling endpoint."""
        return task_scheduling_info(session, task_id)

    return router
