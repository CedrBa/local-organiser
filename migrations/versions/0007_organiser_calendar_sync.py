"""Track external Organiser calendar synchronization for planning blocks.

Revision ID: 0007_organiser_calendar_sync
Revises: 0006_calendar_scheduling_task_load
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0007_organiser_calendar_sync"
down_revision = "0006_calendar_scheduling_task_load"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("planning_blocks") as batch_op:
        batch_op.add_column(sa.Column("external_calendar_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("external_event_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("sync_status", sa.String(), server_default="local_only", nullable=False))
        batch_op.add_column(sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("sync_error", sa.Text(), nullable=True))
        batch_op.create_check_constraint("ck_planning_blocks_sync_status", "sync_status IN ('local_only', 'pending', 'synced', 'failed')")
        batch_op.create_unique_constraint("uq_planning_blocks_external_event_id", ["external_event_id"])
    with op.batch_alter_table("scheduling_proposals") as batch_op:
        batch_op.drop_constraint("ck_scheduling_proposals_status", type_="check")
        batch_op.create_check_constraint("ck_scheduling_proposals_status", "status IN ('proposed', 'syncing', 'applied', 'failed')")


def downgrade() -> None:
    with op.batch_alter_table("scheduling_proposals") as batch_op:
        batch_op.drop_constraint("ck_scheduling_proposals_status", type_="check")
        batch_op.create_check_constraint("ck_scheduling_proposals_status", "status IN ('proposed', 'applied', 'failed')")
    with op.batch_alter_table("planning_blocks") as batch_op:
        batch_op.drop_constraint("uq_planning_blocks_external_event_id", type_="unique")
        batch_op.drop_constraint("ck_planning_blocks_sync_status", type_="check")
        batch_op.drop_column("sync_error")
        batch_op.drop_column("synced_at")
        batch_op.drop_column("sync_status")
        batch_op.drop_column("external_event_id")
        batch_op.drop_column("external_calendar_id")
