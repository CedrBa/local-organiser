"""Add internal scheduling blocks and task split capability.

Revision ID: 0004_scheduling_foundation
Revises: 0003_planner_proposals
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_scheduling_foundation"
down_revision = "0003_planner_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("allow_split", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.create_table(
        "planning_blocks",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("task_id", sa.String(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source_plan_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('planned', 'cancelled', 'completed')", name="ck_planning_blocks_status"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planning_blocks_task_id_starts_at", "planning_blocks", ["task_id", "starts_at"])


def downgrade() -> None:
    op.drop_index("ix_planning_blocks_task_id_starts_at", table_name="planning_blocks")
    op.drop_table("planning_blocks")
    op.drop_column("tasks", "allow_split")
