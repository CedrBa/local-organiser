"""Real temporary SQLite migration/enforcement tests; never uses the runtime DB."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from organiser.db.base import Base
from organiser.db.migrations import PROJECT_ROOT
from organiser.db.models import Resource, ResourceSnapshot, Topic

OLD = "0009_context_knowledge_path_unique"
NEW = "0010_context_workspace"
NEW_TABLES = {
    "resources",
    "resource_snapshots",
    "topics",
    "resource_topics",
    "task_resources",
    "context_action_proposals",
}
STAMP = "2026-09-30 10:00:00"


def database(tmp_path, revision=NEW):
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{tmp_path / 'schema.sqlite3'}")
    command.upgrade(config, revision)
    engine = create_engine(config.get_main_option("sqlalchemy.url"))

    @event.listens_for(engine, "connect")
    def enable_fk(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    return config, engine


def insert(connection, table, **values):
    columns = ", ".join(f'"{name}"' for name in values)
    connection.exec_driver_sql(
        f'INSERT INTO "{table}" ({columns}) VALUES ({", ".join("?" for _ in values)})',
        tuple(values.values()),
    )


def legacy_world(engine):
    with engine.begin() as c:
        insert(
            c,
            "inbox",
            id="i",
            created_at=STAMP,
            received_at=STAMP,
            text="Bilanz",
            source="api",
            raw_payload='{"text":"original"}',
            processing_status="processed",
            processed_at=STAMP,
        )
        insert(
            c,
            "projects",
            id="p",
            title="Bilanz",
            description="Original",
            status="active",
            created_at=STAMP,
            updated_at=STAMP,
        )
        for identifier, parent in (("t", None), ("child", "t")):
            insert(
                c,
                "tasks",
                id=identifier,
                title="Bilanz",
                status="open",
                project_id="p",
                parent_task_id=parent,
                source_inbox_id="i",
                created_at=STAMP,
                updated_at=STAMP,
            )
        insert(
            c,
            "processing_runs",
            id="run",
            run_type="inbox",
            status="completed",
            source_inbox_id="i",
            output_payload='{"task":"t"}',
            created_at=STAMP,
        )
        insert(
            c,
            "planning_blocks",
            id="block",
            task_id="t",
            starts_at=STAMP,
            ends_at=STAMP,
            status="planned",
            created_at=STAMP,
            updated_at=STAMP,
        )
        for identifier, parent, path in (("c", None, "contexts/c"), ("c2", "c", None)):
            insert(
                c,
                "contexts",
                id=identifier,
                title="Bilanz",
                type="course",
                status="active",
                parent_context_id=parent,
                knowledge_path=path,
                created_at=STAMP,
                updated_at=STAMP,
            )
        insert(
            c,
            "context_sources",
            id="s",
            context_id="c",
            source_type="folder",
            path="/allowed/course",
            status="active",
            created_at=STAMP,
            updated_at=STAMP,
        )
        for identifier, cause in ((1, None), (2, 1)):
            insert(
                c,
                "events",
                id=identifier,
                occurred_at=STAMP,
                event_type="task.created",
                entity_type="task",
                entity_id="t",
                source_type="api",
                source_id="i",
                correlation_id="corr",
                causation_event_id=cause,
                payload='{"original":[1,"ä"]}',
            )


def rows(engine, tables, columns=None):
    with engine.connect() as c:
        return {
            table: [
                tuple(row)
                for row in c.exec_driver_sql(
                    f'SELECT {", ".join(columns[table]) if columns else "*"} FROM "{table}" ORDER BY 1'
                )
            ]
            for table in tables
        }


def seed_workspace(engine):
    with engine.begin() as c:
        insert(
            c,
            "resources",
            id="r",
            context_id="c",
            source_id="s",
            locator="/allowed/course/a.md",
            relative_path="a.md",
            source_path_snapshot="/allowed/course",
            title="Bilanz",
            resource_type="markdown",
            current_content_hash="a" * 64,
            byte_size=12,
            mtime_ns=123,
            last_seen_at=STAMP,
            created_at=STAMP,
            updated_at=STAMP,
        )
        insert(
            c,
            "resource_snapshots",
            id="snap",
            resource_id="r",
            content_hash="a" * 64,
            extractor_version="text-v1",
            status="ready",
            text="Bilanzierung",
            metadata='{"page_spans":[]}',
            created_at=STAMP,
        )
        insert(
            c,
            "topics",
            id="topic",
            context_id="c",
            title="Bilanz",
            title_key="bilanz",
            knowledge_key="topic-topic",
            created_at=STAMP,
            updated_at=STAMP,
        )
        c.exec_driver_sql("UPDATE tasks SET context_id='c', topic_id='topic' WHERE id='t'")
        insert(c, "resource_topics", resource_id="r", topic_id="topic", created_at=STAMP)
        insert(c, "task_resources", task_id="t", resource_id="r", created_at=STAMP)
        insert(
            c,
            "context_action_proposals",
            id="proposal",
            context_id="c",
            processing_run_id="run",
            source_inbox_id="i",
            resource_snapshot_id="snap",
            action_payload='{"action":"create_topic"}',
            packet_payload='{"manifest":{}}',
            preconditions='{"hash":"original"}',
            created_at=STAMP,
        )


def test_upgrade_preserves_all_legacy_columns_and_knowledge(tmp_path):
    config, engine = database(tmp_path, OLD)
    legacy_world(engine)
    tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
    columns = {
        table: [col["name"] for col in inspect(engine).get_columns(table)] for table in tables
    }
    before = rows(engine, tables)
    knowledge = tmp_path / "overview.md"
    knowledge.write_bytes(b"Qualitativer Lernstand: ca. 75 %\n")
    original_bytes = knowledge.read_bytes()
    command.upgrade(config, NEW)
    assert rows(engine, tables, columns) == before
    assert knowledge.read_bytes() == original_bytes
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT context_id, topic_id FROM tasks").all() == [
            (None, None),
            (None, None),
        ]
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    engine.dispose()


def test_fresh_database_single_head_and_complete_orm_parity(tmp_path):
    config, engine = database(tmp_path)
    assert ScriptDirectory.from_config(config).get_heads() == [NEW]
    assert NEW_TABLES <= set(inspect(engine).get_table_names())
    with engine.connect() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert c.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one() == NEW
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        assert compare_metadata(MigrationContext.configure(c), Base.metadata) == []
        for table in NEW_TABLES | {"tasks"}:
            expected = {
                constraint.name: str(constraint.sqltext)
                for constraint in Base.metadata.tables[table].constraints
                if hasattr(constraint, "sqltext")
            }
            actual = {
                constraint["name"]: constraint["sqltext"]
                for constraint in inspect(c).get_check_constraints(table)
            }
            assert actual == expected
    engine.dispose()


@pytest.fixture
def populated(tmp_path):
    config, engine = database(tmp_path)
    legacy_world(engine)
    seed_workspace(engine)
    yield config, engine
    engine.dispose()


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE resources SET context_id='absent'",
        "UPDATE resources SET source_id='absent'",
        "UPDATE resources SET status='unknown'",
        "UPDATE resources SET resource_type='html'",
        "UPDATE resources SET byte_size=-1",
        "UPDATE resources SET current_content_hash='bad'",
        "UPDATE resource_snapshots SET resource_id='absent'",
        "UPDATE resource_snapshots SET status='unknown'",
        "UPDATE resource_snapshots SET content_hash='g' || substr(content_hash, 2)",
        "UPDATE resource_snapshots SET extractor_version=''",
        "UPDATE resource_snapshots SET text=replace(hex(zeroblob(100001)), '0', 'x')",
        "UPDATE resource_snapshots SET status='failed'",
        "UPDATE resource_snapshots SET truncated=2",
        "UPDATE topics SET context_id='absent'",
        "UPDATE topics SET status='75%'",
        "UPDATE topics SET knowledge_key='renamed'",
        "UPDATE topics SET title_key=''",
        "UPDATE tasks SET context_id='absent' WHERE id='t'",
        "UPDATE tasks SET topic_id='absent' WHERE id='t'",
        "UPDATE tasks SET context_id=NULL WHERE id='t'",
        "UPDATE resource_topics SET resource_id='absent'",
        "UPDATE resource_topics SET topic_id='absent'",
        "UPDATE task_resources SET task_id='absent'",
        "UPDATE task_resources SET resource_id='absent'",
        "UPDATE context_action_proposals SET context_id='absent'",
        "UPDATE context_action_proposals SET processing_run_id='absent'",
        "UPDATE context_action_proposals SET source_inbox_id='absent'",
        "UPDATE context_action_proposals SET resource_snapshot_id='absent'",
        "UPDATE context_action_proposals SET status='unknown'",
        "INSERT INTO resource_topics SELECT * FROM resource_topics",
        "INSERT INTO task_resources SELECT * FROM task_resources",
        "INSERT INTO task_resources VALUES ('absent', 'r', '2026-09-30 10:00:00')",
        "INSERT INTO resource_topics VALUES ('r', 'absent', '2026-09-30 10:00:00')",
        "INSERT INTO topics SELECT 'bad', 'absent', title, 'new', status, 'topic-bad', created_at, updated_at FROM topics",
        "INSERT INTO topics SELECT 'bad', context_id, title, 'new', '75%', 'topic-bad', created_at, updated_at FROM topics",
        "INSERT INTO resource_snapshots SELECT 'bad', resource_id, content_hash, 'new-version', 'unknown', NULL, truncated, metadata, error_code, created_at FROM resource_snapshots",
        "INSERT INTO resources SELECT 'duplicate', context_id, source_id, locator, relative_path, source_path_snapshot, title, resource_type, status, current_content_hash, byte_size, mtime_ns, last_seen_at, created_at, updated_at FROM resources",
        "INSERT INTO resource_snapshots SELECT 'duplicate', resource_id, content_hash, extractor_version, status, text, truncated, metadata, error_code, created_at FROM resource_snapshots",
        "INSERT INTO topics SELECT 'duplicate', context_id, title, title_key, status, 'topic-duplicate', created_at, updated_at FROM topics",
    ],
)
def test_constraints_reject_actual_invalid_writes(populated, statement):
    _, engine = populated
    with engine.begin() as c:
        assert c.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        with pytest.raises(IntegrityError):
            c.exec_driver_sql(statement)
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_source_removal_retains_inventory_snapshots_links_and_proposals(populated):
    _, engine = populated
    before = rows(engine, NEW_TABLES - {"resources"})
    with engine.begin() as c:
        c.exec_driver_sql("DELETE FROM context_sources WHERE id='s'")
        assert c.exec_driver_sql("SELECT source_id, source_path_snapshot FROM resources").one() == (
            None,
            "/allowed/course",
        )
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
    assert rows(engine, NEW_TABLES - {"resources"}) == before


def test_identity_scope_versions_and_nullable_tasks(populated):
    _, engine = populated
    with engine.begin() as c:
        c.exec_driver_sql(
            "INSERT INTO topics SELECT 'topic2', 'c2', title, title_key, status, 'topic-topic2', created_at, updated_at FROM topics"
        )
        c.exec_driver_sql(
            "INSERT INTO resources SELECT 'r2', 'c2', NULL, locator, relative_path, source_path_snapshot, title, resource_type, status, current_content_hash, byte_size, mtime_ns, last_seen_at, created_at, updated_at FROM resources"
        )
        c.exec_driver_sql(
            "INSERT INTO resource_snapshots SELECT 'snap2', resource_id, content_hash, 'text-v2', status, text, truncated, metadata, error_code, created_at FROM resource_snapshots"
        )
        c.exec_driver_sql("UPDATE tasks SET topic_id=NULL, context_id=NULL")
        assert (
            c.exec_driver_sql("SELECT count(*) FROM tasks WHERE context_id IS NULL").scalar_one()
            == 2
        )
        # Same-context equality is a service obligation, explicitly not promised by these FKs.
        c.exec_driver_sql("UPDATE tasks SET context_id='c', topic_id='topic2' WHERE id='t'")
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []


def test_orm_metadata_alias_and_defaults(populated):
    _, engine = populated
    with Session(engine) as session:
        snapshot = session.get(ResourceSnapshot, "snap")
        assert snapshot.metadata_payload == {"page_spans": []}
        resource = Resource(
            context_id="c",
            locator="/allowed/new.txt",
            relative_path="new.txt",
            source_path_snapshot="/allowed",
            title="New",
            resource_type="text",
        )
        session.add(resource)
        session.flush()
        topic = Topic(
            id="new-topic",
            context_id="c",
            title="New",
            title_key="new",
            knowledge_key="topic-new-topic",
        )
        session.add(topic)
        session.commit()
        assert resource.id and resource.created_at and resource.status == "active"
        assert topic.status == "not_started"


def test_downgrade_reupgrade_loses_only_new_schema_data(populated):
    config, engine = populated
    legacy_tables = set(inspect(engine).get_table_names()) - NEW_TABLES - {"alembic_version"}
    columns = {
        table: [
            col["name"]
            for col in inspect(engine).get_columns(table)
            if table != "tasks" or col["name"] not in {"context_id", "topic_id"}
        ]
        for table in legacy_tables
    }
    before = rows(engine, legacy_tables, columns)
    command.downgrade(config, OLD)
    assert not NEW_TABLES & set(inspect(engine).get_table_names())
    assert rows(engine, legacy_tables) == before
    command.upgrade(config, NEW)
    assert rows(engine, legacy_tables, columns) == before
    assert all(not data for data in rows(engine, NEW_TABLES).values())
    with engine.connect() as c:
        assert c.exec_driver_sql("SELECT context_id, topic_id FROM tasks").all() == [
            (None, None),
            (None, None),
        ]
        assert c.exec_driver_sql("PRAGMA foreign_key_check").all() == []
