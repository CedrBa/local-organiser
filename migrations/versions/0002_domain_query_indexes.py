"""Add indexes for Phase 2 domain and history queries.

Revision ID: 0002_domain_query_indexes
Revises: 0001_phase1_foundation
Create Date: 2026-09-17
"""

from alembic import op


revision = "0002_domain_query_indexes"
down_revision = "0001_phase1_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_tasks_status", "tasks", ["status"])
    op.create_index("ix_tasks_project_id", "tasks", ["project_id"])
    op.create_index("ix_tasks_parent_task_id", "tasks", ["parent_task_id"])
    op.create_index("ix_tasks_due_at", "tasks", ["due_at"])
    op.create_index("ix_events_entity_type_entity_id", "events", ["entity_type", "entity_id"])
    op.create_index("ix_events_correlation_id", "events", ["correlation_id"])


def downgrade() -> None:
    op.drop_index("ix_events_correlation_id", table_name="events")
    op.drop_index("ix_events_entity_type_entity_id", table_name="events")
    op.drop_index("ix_tasks_due_at", table_name="tasks")
    op.drop_index("ix_tasks_parent_task_id", table_name="tasks")
    op.drop_index("ix_tasks_project_id", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
