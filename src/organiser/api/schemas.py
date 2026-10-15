from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ResponseModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class EventContextInput(BaseModel):
    source_type: str | None = None
    source_id: str | None = None
    correlation_id: str | None = None
    causation_event_id: int | None = Field(default=None, gt=0)


class StrictEventContextInput(EventContextInput):
    """An ``EventContextInput`` that rejects unknown fields instead of ignoring them.

    Why a strict variant exists
    --------------------------
    On the routes whose body carries *nothing but* the event context, the flat
    spelling is the documented one. ``EventContextInput`` itself ignores extra keys,
    so a caller sending the nested spelling used by the other C bodies
    (``{"event_context": {"source_id": "..."}}``) got a ``200`` and an audit log with
    ``source_id IS NULL``: the provenance was silently dropped. A silent no-op is
    worse than a rejection here, because the caller believes the operation was
    attributed to their proposal when it was not.

    ``extra="forbid"`` turns that into an explicit ``422``. The flat, direct-body
    spelling keeps working unchanged, so this is purely additive for callers that
    follow the contract.

    Only the ensure routes use it. The link routes that also take a bare
    ``EventContextInput`` body are deliberately left as they are: changing them
    would alter an already-shipped request contract, and C's own convention there is
    already the flat spelling.
    """

    model_config = ConfigDict(extra="forbid")


class ProjectCreate(BaseModel):
    title: str
    description: str | None = None
    event_context: EventContextInput | None = None


class ProjectUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    event_context: EventContextInput | None = None


class ProjectResponse(ResponseModel):
    id: str
    title: str
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime


class TaskCreate(BaseModel):
    title: str
    description: str | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    earliest_start_at: datetime | None = None
    due_at: datetime | None = None
    estimated_minutes: int | None = None
    allow_split: bool = False
    cognitive_load: Literal["high", "medium", "low", "physical"] = "medium"
    source_inbox_id: str | None = None
    # Auslassen und explizites null sind hier *verschiedene* Eingaben. Der
    # Router muss deshalb ``exclude_unset=True`` verwenden: erst dann erreicht
    # ``UNSET`` den Service und die einmalige Uebernahme des Parent-Context
    # bleibt unterscheidbar von einem ausdruecklichen "kein Context".
    context_id: str | None = None
    topic_id: str | None = None
    event_context: EventContextInput | None = None


class TaskUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    estimated_minutes: int | None = None
    allow_split: bool | None = None
    cognitive_load: Literal["high", "medium", "low", "physical"] | None = None
    project_id: str | None = None
    parent_task_id: str | None = None
    # Hier ist null bedeutsam und erlaubt: es entfernt den Bezug bewusst.
    # Deshalb gibt es bewusst kein reject-null fuer diese beiden Felder.
    context_id: str | None = None
    topic_id: str | None = None
    event_context: EventContextInput | None = None

    @field_validator("title", "cognitive_load")
    @classmethod
    def reject_null_required_details(cls, value):
        if value is None:
            raise ValueError("das Feld darf nicht null sein")
        return value

    @field_validator("allow_split")
    @classmethod
    def require_allow_split_boolean(cls, value: bool | None) -> bool | None:
        if value is None:
            raise ValueError("allow_split darf nicht null sein")
        return value


class TaskReschedule(BaseModel):
    earliest_start_at: datetime | None = None
    due_at: datetime | None = None
    event_context: EventContextInput | None = None


class TaskResponse(ResponseModel):
    id: str
    title: str
    description: str | None
    status: str
    project_id: str | None
    parent_task_id: str | None
    earliest_start_at: datetime | None
    due_at: datetime | None
    estimated_minutes: int | None
    allow_split: bool
    cognitive_load: Literal["high", "medium", "low", "physical"]
    source_inbox_id: str | None
    context_id: str | None
    topic_id: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


ContextStatus = Literal["active", "on_hold", "completed", "cancelled"]
ContextSourceType = Literal["manual", "document", "folder", "note", "link", "inbox"]
ContextSourceStatus = Literal["active", "missing", "archived"]


class ContextCreate(BaseModel):
    title: str
    type: str = Field(min_length=1)
    description: str | None = None
    parent_context_id: str | None = None
    knowledge_path: str | None = None
    event_context: EventContextInput | None = None


class ContextUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    type: str | None = None
    parent_context_id: str | None = None
    knowledge_path: str | None = None
    event_context: EventContextInput | None = None

    @field_validator("title", "type")
    @classmethod
    def reject_null(cls, value: str | None) -> str | None:
        """Auslassen bleibt erlaubt; explizites null wuerde einen Pflichtwert loeschen."""
        if value is None:
            raise ValueError("das Feld darf nicht null sein")
        return value


class ContextStatusUpdate(BaseModel):
    status: ContextStatus
    event_context: EventContextInput | None = None


class ContextResponse(ResponseModel):
    id: str
    title: str
    description: str | None
    type: str
    status: ContextStatus
    parent_context_id: str | None
    knowledge_path: str | None
    created_at: datetime
    updated_at: datetime


class ContextSourceCreate(BaseModel):
    source_type: ContextSourceType
    path: str = Field(min_length=1)
    status: ContextSourceStatus = "active"
    event_context: EventContextInput | None = None


class ContextSourceUpdate(BaseModel):
    source_type: ContextSourceType | None = None
    path: str | None = None
    status: ContextSourceStatus | None = None
    event_context: EventContextInput | None = None

    @field_validator("source_type", "path", "status")
    @classmethod
    def reject_null(cls, value):
        if value is None:
            raise ValueError("das Feld darf nicht null sein")
        return value


class ContextSourceResponse(ResponseModel):
    id: str
    context_id: str
    source_type: ContextSourceType
    path: str
    status: ContextSourceStatus
    created_at: datetime
    updated_at: datetime


TopicStatusValue = Literal["not_started", "in_progress", "done", "blocked"]


class TopicCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    status: TopicStatusValue = "not_started"
    event_context: EventContextInput | None = None


class TopicUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Nur der fachliche Titel ist aenderbar. ``knowledge_key`` und die ID sind
    # weder im Eingabemodell noch in der Response aenderbar; ein Rename ist eine
    # reine Beschriftung und verschiebt keine Datei.
    title: str | None = Field(default=None, min_length=1, max_length=200)
    event_context: EventContextInput | None = None

    @field_validator("title")
    @classmethod
    def reject_null_title(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("title darf nicht null sein")
        return value


class TopicStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TopicStatusValue
    event_context: EventContextInput | None = None


class TopicResponse(ResponseModel):
    id: str
    context_id: str
    title: str
    title_key: str
    status: TopicStatusValue
    knowledge_key: str
    created_at: datetime
    updated_at: datetime


class ResourceLinkResponse(ResponseModel):
    id: str
    context_id: str
    title: str
    resource_type: Literal["pdf", "markdown", "text", "other"]
    status: Literal["active", "missing", "archived"]
    relative_path: str


class TaskResourceResponse(ResponseModel):
    task_id: str
    resource_id: str


class ResourceLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource_id: str = Field(min_length=1)
    event_context: EventContextInput | None = None


class TopicKnowledgeUpdateRequest(BaseModel):
    """Qualitativer Fortschritt zu einem bestehenden DB-Topic.

    Bewusst expressiv unmoeglich sind: ``path``, ``storage_key``, ``filename``,
    ``percent_db`` und jeder andere Adress- oder Statusvorschlag. ``extra="forbid"``
    lehnt sie ab, statt sie zu ignorieren. Die physische Adresse entsteht allein
    aus dem ``knowledge_key`` des Topics in der Datenbank.
    """

    model_config = ConfigDict(extra="forbid")

    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    percent: int | None = Field(default=None, ge=0, le=100, strict=True)
    event_context: EventContextInput | None = None


class AvailabilityWindowInput(BaseModel):
    starts_at: datetime
    ends_at: datetime


class AvailabilityNormalizeRequest(BaseModel):
    windows: list[AvailabilityWindowInput]


class SchedulingProposalCreate(BaseModel):
    task_ids: list[str] = Field(min_length=1)
    availability: list[AvailabilityWindowInput]
    horizon_starts_at: datetime
    horizon_ends_at: datetime


class CalendarSchedulingProposalCreate(BaseModel):
    task_ids: list[str] = Field(min_length=1)
    horizon_starts_at: datetime
    horizon_ends_at: datetime


class PlanningBlockCreate(BaseModel):
    task_id: str
    starts_at: datetime
    ends_at: datetime
    source_plan_id: str | None = None


class PlanningBlockResponse(ResponseModel):
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


class TaskSchedulingResponse(BaseModel):
    id: str
    earliest_start_at: datetime | None
    due_at: datetime | None
    estimated_minutes: int | None
    allow_split: bool
    cognitive_load: Literal["high", "medium", "low", "physical"]
    planning_blocks: list[PlanningBlockResponse]


class EventResponse(ResponseModel):
    id: int
    occurred_at: datetime
    event_type: str
    entity_type: str | None
    entity_id: str | None
    source_type: str | None
    source_id: str | None
    correlation_id: str | None
    causation_event_id: int | None
    payload: dict[str, Any] | list[Any] | None
