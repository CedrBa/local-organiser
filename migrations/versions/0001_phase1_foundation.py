"""Create the phase 1 foundation while preserving the existing inbox.

Revision ID: 0001_phase1_foundation
Revises:
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_phase1_foundation"
down_revision = None
branch_labels = None
depends_on = None


def _create_inbox() -> None:
    op.create_table(
        "inbox",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("raw_payload", sa.JSON(), nullable=True),
        sa.Column("processing_status", sa.String(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "processing_status IN ('pending', 'processing', 'processed', 'failed')",
            name="ck_inbox_processing_status",
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "inbox" not in inspector.get_table_names():
        _create_inbox()
    else:
        existing_columns = {column["name"] for column in inspector.get_columns("inbox")}
        additions = {
            "received_at": sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
            "source": sa.Column("source", sa.String(), nullable=True),
            "raw_payload": sa.Column("raw_payload", sa.JSON(), nullable=True),
            "processing_status": sa.Column("processing_status", sa.String(), nullable=True),
            "processed_at": sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            "last_error": sa.Column("last_error", sa.Text(), nullable=True),
        }
        with op.batch_alter_table("inbox") as batch_op:
            for name, column in additions.items():
                if name not in existing_columns:
                    batch_op.add_column(column)

        inbox = sa.table(
            "inbox",
            sa.column("created_at"),
            sa.column("received_at"),
            sa.column("source"),
            sa.column("processing_status"),
        )
        op.execute(inbox.update().where(inbox.c.received_at.is_(None)).values(received_at=inbox.c.created_at))
        op.execute(inbox.update().where(inbox.c.source.is_(None)).values(source="legacy"))
        op.execute(inbox.update().where(inbox.c.processing_status.is_(None)).values(processing_status="pending"))

    op.create_table(
        "projects",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'on_hold', 'completed', 'cancelled')", name="ck_projects_status"),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=True),
        sa.Column("parent_task_id", sa.String(), sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("earliest_start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("estimated_minutes", sa.Integer(), nullable=True),
        sa.Column("source_inbox_id", sa.String(), sa.ForeignKey("inbox.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('open', 'in_progress', 'waiting', 'done', 'cancelled')", name="ck_tasks_status"),
    )
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=True),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("source_type", sa.String(), nullable=True),
        sa.Column("source_id", sa.String(), nullable=True),
        sa.Column("correlation_id", sa.String(), nullable=True),
        sa.Column("causation_event_id", sa.Integer(), sa.ForeignKey("events.id"), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
    )
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("run_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_inbox_id", sa.String(), sa.ForeignKey("inbox.id"), nullable=True),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("context_event_max_id", sa.Integer(), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("processing_runs")
    op.drop_table("events")
    op.drop_table("tasks")
    op.drop_table("projects")
    with op.batch_alter_table("inbox") as batch_op:
        batch_op.drop_column("last_error")
        batch_op.drop_column("processed_at")
        batch_op.drop_column("processing_status")
        batch_op.drop_column("raw_payload")
        batch_op.drop_column("source")
        batch_op.drop_column("received_at")
