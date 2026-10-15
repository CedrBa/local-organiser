"""Persist deterministic scheduling proposals.

Revision ID: 0005_scheduling_proposals
Revises: 0004_scheduling_foundation
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0005_scheduling_proposals"
down_revision = "0004_scheduling_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduling_proposals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("task_ids", sa.JSON(), nullable=False),
        sa.Column("availability_payload", sa.JSON(), nullable=False),
        sa.Column("policy_payload", sa.JSON(), nullable=False),
        sa.Column("proposal_payload", sa.JSON(), nullable=False),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("is_replan", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.CheckConstraint("status IN ('proposed', 'applied', 'failed')", name="ck_scheduling_proposals_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scheduling_proposals_status_created_at", "scheduling_proposals", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_scheduling_proposals_status_created_at", table_name="scheduling_proposals")
    op.drop_table("scheduling_proposals")
