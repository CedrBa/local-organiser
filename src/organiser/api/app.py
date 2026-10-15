"""Application lifecycle and dependency wiring for the protected Local Organiser API."""

from organiser.api.inbox import build_router as build_inbox_router
from organiser.api.planning import build_router as build_planning_router
from organiser.api.scheduling import build_router as build_scheduling_router
from organiser.api.calendar import build_router as build_calendar_router
from organiser.api.projects import build_router as build_projects_router
from organiser.api.tasks import build_router as build_tasks_router
from organiser.api.contexts import build_router as build_contexts_router
from organiser.api.events import build_router as build_events_router
from organiser.api.inbox_input import (
    InboxItem,
    inbox_file_validation_detail,
    with_shortcut_created_at,
)
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging

from fastapi import APIRouter, Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.orm import Session

from organiser.core.config import Settings, get_settings
from organiser.api.security import build_api_token_dependency
from organiser.db.migrations import upgrade_database
from organiser.db.session import create_session_factory
from organiser.api.schemas import (
    EventContextInput,
)
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
)
from organiser.api.context_actions import build_router as build_actions_router
from organiser.api.resources import build_router as build_resources_router
from organiser.api.topics import build_router as build_topics_router
from organiser.api.workspace import build_router as build_workspace_router
from organiser.services.context_knowledge import shared_knowledge_store
from organiser.services.inbox import store_inbox_item
from organiser.services.calendar import CalendarUnavailableError, ICloudCalDAVAdapter
from organiser.services.scheduling import DEFAULT_SCHEDULING_POLICY


logger = logging.getLogger(__name__)


# Der iPhone-Shortcut sendet created_at in der lokalen Schreibweise des Geraets
# (zum Beispiel "26.09.26, 13:42") statt als ISO-8601 mit Zeitzone.


def create_app(settings: Settings | None = None, llm_client=None, calendar_adapter=None) -> FastAPI:
    """Build the API with explicit settings and replaceable model/calendar adapters.

    Startup applies pending migrations to the configured database. All business
    routers inherit token authentication; creating the app is not a readiness check."""
    app_settings = settings or get_settings()
    session_factory = create_session_factory(app_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if not app_settings.api_token:
            logger.warning(
                "ORGANISER_API_TOKEN ist nicht gesetzt: Alle geschuetzten API-Routen antworten mit 503."
            )
        upgrade_database(app_settings)
        yield

    def get_database_session():
        with session_factory() as session:
            yield session

    def get_store():
        return shared_knowledge_store(app_settings)

    app = FastAPI(title="Local Organiser API", lifespan=lifespan)

    # Alle fachlichen Routen liegen auf diesem Router und erben die Bearer-Token-Dependency.
    # /health bleibt bewusst direkt auf der App und damit ohne Auth erreichbar.
    protected = APIRouter(dependencies=[Depends(build_api_token_dependency(app_settings))])

    def event_context(value: EventContextInput | None) -> EventContext | None:
        return EventContext(**value.model_dump()) if value is not None else None

    def calendar_policy():
        return replace(DEFAULT_SCHEDULING_POLICY, calendar_timezone=app_settings.calendar_timezone)

    def configured_calendar_adapter():
        return calendar_adapter or ICloudCalDAVAdapter.from_settings(app_settings)

    @app.exception_handler(NotFoundError)
    async def not_found_handler(_, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainValidationError)
    async def validation_handler(_, exc: DomainValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(CalendarUnavailableError)
    async def calendar_unavailable_handler(_, exc: CalendarUnavailableError):
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @protected.post("/inbox-file")
    async def add_file_to_inbox(
        file: UploadFile = File(...), session: Session = Depends(get_database_session)
    ) -> dict[str, bool | str]:
        try:
            content = await file.read()
            data = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPException(
                status_code=400, detail="Die hochgeladene Datei ist kein gueltiges JSON."
            )

        try:
            item = InboxItem.model_validate(
                with_shortcut_created_at(data, app_settings.calendar_timezone)
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=inbox_file_validation_detail(data, exc))

        store_inbox_item(
            session,
            item_id=item.id,
            created_at=item.created_at,
            text=item.text,
            raw_payload=item.model_dump(mode="json"),
        )
        return {"received": True, "id": item.id}

    protected.include_router(build_inbox_router(app_settings, get_database_session, llm_client))
    protected.include_router(
        build_planning_router(app_settings, get_database_session, get_store, llm_client)
    )
    protected.include_router(build_scheduling_router(get_database_session))
    protected.include_router(
        build_calendar_router(calendar_policy, configured_calendar_adapter, get_database_session)
    )
    protected.include_router(build_projects_router(event_context, get_database_session))
    protected.include_router(build_tasks_router(event_context, get_database_session, get_store))
    protected.include_router(build_contexts_router(event_context, get_database_session))
    protected.include_router(build_events_router(get_database_session))

    # Workspace-Router (B/C/D/E): ohne eigene Auth, nur unter dem geschuetzten Router montiert.
    protected.include_router(build_resources_router(get_database_session, app_settings))
    protected.include_router(build_topics_router(get_database_session, get_store))
    protected.include_router(build_workspace_router(get_database_session, get_store))
    protected.include_router(
        build_actions_router(get_database_session, get_store, app_settings, llm_client)
    )

    app.include_router(protected)

    return app


app = create_app()
