"""Add context workspace inventory, topics and single-action proposals.

Downgrade destroys all workspace rows and task context/topic assignments.
"""
from alembic import op
import sqlalchemy as sa

revision = "0010_context_workspace"
down_revision = "0009_context_knowledge_path_unique"
branch_labels = None
depends_on = None


def _id():
    return sa.Column("id", sa.String(), primary_key=True)


def _fk(name, target, nullable=False, ondelete=None, primary_key=False):
    return sa.Column(name, sa.String(), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable, primary_key=primary_key)


def _stamp(name):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False)


def upgrade():
    op.create_table(
        "resources", _id(), _fk("context_id", "contexts.id"),
        _fk("source_id", "context_sources.id", nullable=True, ondelete="SET NULL"),
        *[sa.Column(n, sa.String(), nullable=False) for n in ("locator", "relative_path", "source_path_snapshot", "title", "resource_type")],
        sa.Column("status", sa.String(), nullable=False, server_default="active"),
        sa.Column("current_content_hash", sa.String(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True), sa.Column("mtime_ns", sa.Integer(), nullable=True),
        _stamp("last_seen_at"), _stamp("created_at"), _stamp("updated_at"),
        sa.UniqueConstraint("context_id", "locator", name="uq_resources_context_locator"),
        sa.CheckConstraint("resource_type IN ('pdf', 'markdown', 'text', 'other')", name="ck_resources_type"),
        sa.CheckConstraint("status IN ('active', 'missing', 'archived')", name="ck_resources_status"),
        sa.CheckConstraint("byte_size IS NULL OR byte_size >= 0", name="ck_resources_byte_size"),
        sa.CheckConstraint("current_content_hash IS NULL OR (length(current_content_hash) = 64 AND current_content_hash NOT GLOB '*[^0-9a-f]*')", name="ck_resources_hash"),
    )
    for column in ("context_id", "source_id"):
        op.create_index(f"ix_resources_{column}", "resources", [column])
    op.create_table(
        "resource_snapshots", _id(), _fk("resource_id", "resources.id"),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("extractor_version", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False), sa.Column("text", sa.Text(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("metadata", sa.JSON(), nullable=False), sa.Column("error_code", sa.String(), nullable=True),
        _stamp("created_at"),
        sa.UniqueConstraint("resource_id", "content_hash", "extractor_version", name="uq_resource_snapshots_version"),
        sa.CheckConstraint("status IN ('ready', 'unsupported', 'failed')", name="ck_resource_snapshots_status"),
        sa.CheckConstraint("length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'", name="ck_resource_snapshots_hash"),
        sa.CheckConstraint("length(extractor_version) > 0", name="ck_resource_snapshots_extractor"),
        sa.CheckConstraint("text IS NULL OR length(text) <= 200000", name="ck_resource_snapshots_text_limit"),
        sa.CheckConstraint("status = 'ready' OR text IS NULL", name="ck_resource_snapshots_failed_text"),
        sa.CheckConstraint("truncated IN (0, 1)", name="ck_resource_snapshots_truncated"),
    )
    op.create_table(
        "topics", _id(), _fk("context_id", "contexts.id"),
        sa.Column("title", sa.String(), nullable=False), sa.Column("title_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="not_started"),
        sa.Column("knowledge_key", sa.String(), nullable=False), _stamp("created_at"), _stamp("updated_at"),
        sa.UniqueConstraint("context_id", "title_key", name="uq_topics_context_title_key"),
        sa.UniqueConstraint("context_id", "knowledge_key", name="uq_topics_context_knowledge_key"),
        sa.CheckConstraint("status IN ('not_started', 'in_progress', 'done', 'blocked')", name="ck_topics_status"),
        sa.CheckConstraint("knowledge_key = 'topic-' || id", name="ck_topics_knowledge_key"),
        sa.CheckConstraint("length(title_key) > 0", name="ck_topics_title_key"),
    )
    op.create_index("ix_topics_context_id", "topics", ["context_id"])
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("context_id", sa.String(), nullable=True))
        batch.add_column(sa.Column("topic_id", sa.String(), nullable=True))
        batch.create_foreign_key("fk_tasks_context_id", "contexts", ["context_id"], ["id"])
        batch.create_foreign_key("fk_tasks_topic_id", "topics", ["topic_id"], ["id"])
        batch.create_check_constraint("ck_tasks_topic_requires_context", "topic_id IS NULL OR context_id IS NOT NULL")
        batch.create_index("ix_tasks_context_id", ["context_id"])
        batch.create_index("ix_tasks_topic_id", ["topic_id"])
    op.create_table("resource_topics", _fk("resource_id", "resources.id", primary_key=True),
                    _fk("topic_id", "topics.id", primary_key=True), _stamp("created_at"))
    op.create_table("task_resources", _fk("task_id", "tasks.id", primary_key=True),
                    _fk("resource_id", "resources.id", primary_key=True), _stamp("created_at"))
    op.create_table(
        "context_action_proposals", _id(), _fk("context_id", "contexts.id"),
        _fk("processing_run_id", "processing_runs.id", nullable=True),
        _fk("source_inbox_id", "inbox.id", nullable=True),
        _fk("resource_snapshot_id", "resource_snapshots.id", nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
        *[sa.Column(n, sa.JSON(), nullable=False) for n in ("action_payload", "packet_payload", "preconditions")],
        sa.Column("model", sa.String(), nullable=True), sa.Column("raw_output", sa.Text(), nullable=True),
        sa.Column("result_payload", sa.JSON(), nullable=True), sa.Column("error_code", sa.String(), nullable=True),
        _stamp("created_at"), sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('proposed', 'applying', 'applied', 'rejected', 'failed', 'ambiguous')", name="ck_context_action_proposals_status"),
    )
    for column in ("context_id", "source_inbox_id", "processing_run_id"):
        op.create_index(f"ix_context_action_proposals_{column}", "context_action_proposals", [column])


def downgrade():
    for table in ("context_action_proposals", "task_resources", "resource_topics"):
        op.drop_table(table)
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_context_id")
        batch.drop_index("ix_tasks_topic_id")
        batch.drop_constraint("ck_tasks_topic_requires_context", type_="check")
        batch.drop_constraint("fk_tasks_context_id", type_="foreignkey")
        batch.drop_constraint("fk_tasks_topic_id", type_="foreignkey")
        batch.drop_column("topic_id")
        batch.drop_column("context_id")
    for table in ("topics", "resource_snapshots", "resources"):
        op.drop_table(table)
