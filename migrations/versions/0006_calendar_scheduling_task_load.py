"""Add task cognitive load for calendar-aware scheduling.

Revision ID: 0006_calendar_scheduling_task_load
Revises: 0005_scheduling_proposals
Create Date: 2026-09-17
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_calendar_scheduling_task_load"
down_revision = "0005_scheduling_proposals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("cognitive_load", sa.String(), server_default="medium", nullable=False))
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint("ck_tasks_cognitive_load", "cognitive_load IN ('high', 'medium', 'low', 'physical')")


def downgrade() -> None:
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_cognitive_load", type_="check")
    op.drop_column("tasks", "cognitive_load")
