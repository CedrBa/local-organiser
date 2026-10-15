"""Persist planner proposals and their bounded context.

Revision ID: 0003_planner_proposals
Revises: 0002_domain_query_indexes
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_planner_proposals"
down_revision = "0002_domain_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planner_proposals",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("target_type", sa.String(), nullable=False),
        sa.Column("target_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("model", sa.String(), nullable=True),
        sa.Column("context_event_max_id", sa.Integer(), nullable=False),
        sa.Column("context_payload", sa.JSON(), nullable=False),
        sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("proposal_payload", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('proposed', 'applied', 'failed')", name="ck_planner_proposals_status"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planner_proposals_target", "planner_proposals", ["target_type", "target_id"])


def downgrade() -> None:
    op.drop_index("ix_planner_proposals_target", table_name="planner_proposals")
    op.drop_table("planner_proposals")
