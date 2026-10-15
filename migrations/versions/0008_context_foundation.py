"""Add persistent contexts and their registered sources.

Revision ID: 0008_context_foundation
Revises: 0007_organiser_calendar_sync
Create Date: 2026-09-29
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_context_foundation"
down_revision = "0007_organiser_calendar_sync"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "contexts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("parent_context_id", sa.String(), nullable=True),
        sa.Column("knowledge_path", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'on_hold', 'completed', 'cancelled')", name="ck_contexts_status"),
        sa.ForeignKeyConstraint(["parent_context_id"], ["contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_contexts_parent_context_id", "contexts", ["parent_context_id"])
    op.create_index("ix_contexts_status", "contexts", ["status"])
    op.create_table(
        "context_sources",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("context_id", sa.String(), nullable=False),
        sa.Column("source_type", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="active", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('manual', 'document', 'folder', 'note', 'link', 'inbox')",
            name="ck_context_sources_source_type",
        ),
        sa.CheckConstraint("status IN ('active', 'missing', 'archived')", name="ck_context_sources_status"),
        sa.ForeignKeyConstraint(["context_id"], ["contexts.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("context_id", "path", name="uq_context_sources_context_id_path"),
    )
    op.create_index("ix_context_sources_context_id", "context_sources", ["context_id"])


def downgrade() -> None:
    op.drop_index("ix_context_sources_context_id", table_name="context_sources")
    op.drop_table("context_sources")
    op.drop_index("ix_contexts_status", table_name="contexts")
    op.drop_index("ix_contexts_parent_context_id", table_name="contexts")
    op.drop_table("contexts")
