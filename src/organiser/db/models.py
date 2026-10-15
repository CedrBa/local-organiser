from datetime import UTC, datetime
import uuid

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from organiser.db.base import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp for new persistent records."""
    return datetime.now(UTC)


def new_id() -> str:
    """Generate a UUID identifier independent of database insertion order."""
    return str(uuid.uuid4())


class Inbox(Base):
    __tablename__ = "inbox"
    __table_args__ = (
        CheckConstraint(
            "processing_status IN ('pending', 'processing', 'processed', 'failed')",
            name="ck_inbox_processing_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    raw_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    processing_status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_hold', 'completed', 'cancelled')",
            name="ck_projects_status",
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'in_progress', 'waiting', 'done', 'cancelled')",
            name="ck_tasks_status",
        ),
        CheckConstraint(
            "cognitive_load IN ('high', 'medium', 'low', 'physical')",
            name="ck_tasks_cognitive_load",
        ),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_project_id", "project_id"),
        Index("ix_tasks_parent_task_id", "parent_task_id"),
        Index("ix_tasks_due_at", "due_at"),
        Index("ix_tasks_context_id", "context_id"),
        Index("ix_tasks_topic_id", "topic_id"),
        CheckConstraint(
            "topic_id IS NULL OR context_id IS NOT NULL", name="ck_tasks_topic_requires_context"
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    project_id: Mapped[str | None] = mapped_column(ForeignKey("projects.id"), nullable=True)
    context_id: Mapped[str | None] = mapped_column(ForeignKey("contexts.id"), nullable=True)
    topic_id: Mapped[str | None] = mapped_column(ForeignKey("topics.id"), nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(ForeignKey("tasks.id"), nullable=True)
    earliest_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    estimated_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_split: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    cognitive_load: Mapped[str] = mapped_column(
        String, nullable=False, default="medium", server_default="medium"
    )
    source_inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inbox.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Event(Base):
    __tablename__ = "events"
    __table_args__ = (
        Index("ix_events_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_events_correlation_id", "correlation_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String, nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String, nullable=True)
    source_type: Mapped[str | None] = mapped_column(String, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    causation_event_id: Mapped[int | None] = mapped_column(ForeignKey("events.id"), nullable=True)
    payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)


class Context(Base):
    """Persistenter Wissensbereich; existiert unabhaengig von registrierten Quellen."""

    __tablename__ = "contexts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'on_hold', 'completed', 'cancelled')", name="ck_contexts_status"
        ),
        Index("ix_contexts_status", "status"),
        Index("ix_contexts_parent_context_id", "parent_context_id"),
        # Ein Context-Speicherort gehoert genau einem Context. NULL bleibt erlaubt
        # und bedeutet "noch nicht gebunden"; der partielle Unique-Index greift
        # deshalb nur fuer gesetzte Werte.
        Index(
            "uq_contexts_knowledge_path",
            "knowledge_path",
            unique=True,
            sqlite_where=text("knowledge_path IS NOT NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Offenes Vokabular: Bereiche wie "Bewerbung" oder "Finanzierung" sind frei waehlbar.
    type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    parent_context_id: Mapped[str | None] = mapped_column(ForeignKey("contexts.id"), nullable=True)
    # Reservierter Speicherort fuer spaeteres Wissen; wird hier nur validiert, nie geschrieben.
    knowledge_path: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ContextSource(Base):
    """Manuell registrierte Quelle eines Contexts; ein Context darf keine Quellen besitzen."""

    __tablename__ = "context_sources"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('manual', 'document', 'folder', 'note', 'link', 'inbox')",
            name="ck_context_sources_source_type",
        ),
        CheckConstraint(
            "status IN ('active', 'missing', 'archived')", name="ck_context_sources_status"
        ),
        Index("ix_context_sources_context_id", "context_id"),
        UniqueConstraint("context_id", "path", name="uq_context_sources_context_id_path"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    context_id: Mapped[str] = mapped_column(ForeignKey("contexts.id"), nullable=False)
    source_type: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class Resource(Base):
    __tablename__ = "resources"
    __table_args__ = (
        UniqueConstraint("context_id", "locator", name="uq_resources_context_locator"),
        CheckConstraint(
            "resource_type IN ('pdf', 'markdown', 'text', 'other')", name="ck_resources_type"
        ),
        CheckConstraint("status IN ('active', 'missing', 'archived')", name="ck_resources_status"),
        CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="ck_resources_byte_size"),
        CheckConstraint(
            "current_content_hash IS NULL OR (length(current_content_hash) = 64 AND current_content_hash NOT GLOB '*[^0-9a-f]*')",
            name="ck_resources_hash",
        ),
        Index("ix_resources_context_id", "context_id"),
        Index("ix_resources_source_id", "source_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    context_id: Mapped[str] = mapped_column(ForeignKey("contexts.id"), nullable=False)
    source_id: Mapped[str | None] = mapped_column(
        ForeignKey("context_sources.id", ondelete="SET NULL"), nullable=True
    )
    locator: Mapped[str] = mapped_column(String, nullable=False)
    relative_path: Mapped[str] = mapped_column(String, nullable=False)
    source_path_snapshot: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    resource_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="active", server_default="active"
    )
    current_content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mtime_ns: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ResourceSnapshot(Base):
    """Immutable, bounded derived cache; qualitative knowledge lives in Markdown."""

    __tablename__ = "resource_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "resource_id", "content_hash", "extractor_version", name="uq_resource_snapshots_version"
        ),
        CheckConstraint(
            "status IN ('ready', 'unsupported', 'failed')", name="ck_resource_snapshots_status"
        ),
        CheckConstraint(
            "length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'",
            name="ck_resource_snapshots_hash",
        ),
        CheckConstraint("length(extractor_version) > 0", name="ck_resource_snapshots_extractor"),
        CheckConstraint(
            "text IS NULL OR length(text) <= 200000", name="ck_resource_snapshots_text_limit"
        ),
        CheckConstraint(
            "status = 'ready' OR text IS NULL", name="ck_resource_snapshots_failed_text"
        ),
        CheckConstraint("truncated IN (0, 1)", name="ck_resource_snapshots_truncated"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)
    extractor_version: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    metadata_payload: Mapped[dict] = mapped_column("metadata", JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Topic(Base):
    __tablename__ = "topics"
    __table_args__ = (
        UniqueConstraint("context_id", "title_key", name="uq_topics_context_title_key"),
        UniqueConstraint("context_id", "knowledge_key", name="uq_topics_context_knowledge_key"),
        CheckConstraint(
            "status IN ('not_started', 'in_progress', 'done', 'blocked')", name="ck_topics_status"
        ),
        CheckConstraint("knowledge_key = 'topic-' || id", name="ck_topics_knowledge_key"),
        CheckConstraint("length(title_key) > 0", name="ck_topics_title_key"),
        Index("ix_topics_context_id", "context_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    context_id: Mapped[str] = mapped_column(ForeignKey("contexts.id"), nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    title_key: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="not_started", server_default="not_started"
    )
    knowledge_key: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ResourceTopic(Base):
    __tablename__ = "resource_topics"

    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    topic_id: Mapped[str] = mapped_column(ForeignKey("topics.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class TaskResource(Base):
    __tablename__ = "task_resources"

    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), primary_key=True)
    resource_id: Mapped[str] = mapped_column(ForeignKey("resources.id"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class ContextActionProposal(Base):
    __tablename__ = "context_action_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'applying', 'applied', 'rejected', 'failed', 'ambiguous')",
            name="ck_context_action_proposals_status",
        ),
        Index("ix_context_action_proposals_context_id", "context_id"),
        Index("ix_context_action_proposals_source_inbox_id", "source_inbox_id"),
        Index("ix_context_action_proposals_processing_run_id", "processing_run_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    context_id: Mapped[str] = mapped_column(ForeignKey("contexts.id"), nullable=False)
    processing_run_id: Mapped[str | None] = mapped_column(
        ForeignKey("processing_runs.id"), nullable=True
    )
    source_inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inbox.id"), nullable=True)
    resource_snapshot_id: Mapped[str | None] = mapped_column(
        ForeignKey("resource_snapshots.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String, nullable=False, default="proposed", server_default="proposed"
    )
    action_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    packet_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    preconditions: Mapped[dict] = mapped_column(JSON, nullable=False)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ProcessingRun(Base):
    __tablename__ = "processing_runs"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    run_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    source_inbox_id: Mapped[str | None] = mapped_column(ForeignKey("inbox.id"), nullable=True)
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    context_event_max_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_payload: Mapped[dict | list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class PlannerProposalRecord(Base):
    __tablename__ = "planner_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'applied', 'failed')", name="ck_planner_proposals_status"
        ),
        Index("ix_planner_proposals_target", "target_type", "target_id"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    target_type: Mapped[str] = mapped_column(String, nullable=False)
    target_id: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    model: Mapped[str | None] = mapped_column(String, nullable=True)
    context_event_max_id: Mapped[int] = mapped_column(Integer, nullable=False)
    context_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposal_payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PlanningBlock(Base):
    __tablename__ = "planning_blocks"
    __table_args__ = (
        CheckConstraint(
            "status IN ('planned', 'cancelled', 'completed')", name="ck_planning_blocks_status"
        ),
        CheckConstraint(
            "sync_status IN ('local_only', 'pending', 'synced', 'failed')",
            name="ck_planning_blocks_sync_status",
        ),
        Index("ix_planning_blocks_task_id_starts_at", "task_id", "starts_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="planned")
    source_plan_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_calendar_id: Mapped[str | None] = mapped_column(String, nullable=True)
    external_event_id: Mapped[str | None] = mapped_column(String, nullable=True, unique=True)
    sync_status: Mapped[str] = mapped_column(
        String, nullable=False, default="local_only", server_default="local_only"
    )
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SchedulingProposalRecord(Base):
    __tablename__ = "scheduling_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('proposed', 'syncing', 'applied', 'failed')",
            name="ck_scheduling_proposals_status",
        ),
        Index("ix_scheduling_proposals_status_created_at", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=new_id)
    status: Mapped[str] = mapped_column(String, nullable=False, default="proposed")
    task_ids: Mapped[list] = mapped_column(JSON, nullable=False)
    availability_payload: Mapped[list] = mapped_column(JSON, nullable=False)
    policy_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    proposal_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    warnings: Mapped[list] = mapped_column(JSON, nullable=False)
    is_replan: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
