"""Migration contracts for contexts."""

from __future__ import annotations

import pytest
from alembic import command
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Context
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    add_context_source,
    create_context,
    create_project,
    create_task,
    get_project,
    get_task,
)
from support.context_foundation import (
    session as session,
    without_timestamps as without_timestamps,
    alembic_config as alembic_config,
    connect as connect,
    table_names as table_names,
    create_legacy_data as create_legacy_data,
    read_legacy_data as read_legacy_data,
    client as client,
)


def test_migration_from_0007_creates_context_tables_and_keeps_legacy_data(tmp_path):
    """Echter 0007->0008-Uebergang: erst Altbestand, dann Migration, dann Pruefung."""
    settings = make_settings(tmp_path, "upgrade-0008.sqlite3")
    config = alembic_config(settings)

    command.upgrade(config, "0007_organiser_calendar_sync")
    session_factory = create_session_factory(settings)
    legacy = create_legacy_data(session_factory)

    with connect(settings).connect() as connection:
        assert not table_names(connection) & {"contexts", "context_sources"}

    command.upgrade(config, "0008_context_foundation")

    with connect(settings).connect() as connection:
        assert {"contexts", "context_sources"} <= table_names(connection)
        assert {"projects", "tasks", "events", "inbox"} <= table_names(connection)
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0008_context_foundation"
        )
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []

    assert read_legacy_data(session_factory, legacy) == {
        "project_title": "Hausarbeit",
        "project_status": "active",
        "task_title": "Gliederung",
        "task_description": "Kapitel 1",
        "task_project_id": legacy["project_id"],
        "event_ids": legacy["event_ids"],
    }


def test_migration_adds_the_documented_constraints(tmp_path):
    """0007->head erzeugt genau die dokumentierten Spalten, Checks, FKs und Unique-Constraint."""
    settings = make_settings(tmp_path, "upgrade-constraints.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0007_organiser_calendar_sync")
    command.upgrade(config, "head")

    with connect(settings).connect() as connection:
        context_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(contexts)")
        }
        source_columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info(context_sources)")
        }
        context_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'contexts'"
        ).scalar_one()
        source_sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'context_sources'"
        ).scalar_one()
        source_foreign_keys = {
            (row[2], row[3], row[4])
            for row in connection.exec_driver_sql("PRAGMA foreign_key_list(context_sources)")
        }
        context_foreign_keys = {
            (row[2], row[3], row[4])
            for row in connection.exec_driver_sql("PRAGMA foreign_key_list(contexts)")
        }

    assert {
        "id",
        "title",
        "description",
        "type",
        "status",
        "parent_context_id",
        "knowledge_path",
        "created_at",
        "updated_at",
    } == context_columns
    assert {
        "id",
        "context_id",
        "source_type",
        "path",
        "status",
        "created_at",
        "updated_at",
    } == source_columns
    assert "ck_contexts_status" in context_sql
    assert "ck_context_sources_source_type" in source_sql
    assert "ck_context_sources_status" in source_sql
    assert "uq_context_sources_context_id_path" in source_sql
    assert ("contexts", "context_id", "id") in source_foreign_keys
    assert ("contexts", "parent_context_id", "id") in context_foreign_keys


def test_migration_rejects_duplicate_source_paths_per_context(tmp_path):
    """Die Unique-Constraint aus 0008 ist in der Datenbank wirksam, nicht nur im Model."""
    settings = make_settings(tmp_path, "upgrade-unique.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "head")
    session_factory = create_session_factory(settings)

    with session_factory() as database_session:
        created = create_context(database_session, title="Bewerbungen", type="topic")
        add_context_source(
            database_session,
            context_id=created.id,
            source_type="document",
            path="uni/finanzierung.pdf",
        )
        context_id = created.id

    with connect(settings).begin() as connection:
        with pytest.raises(IntegrityError) as excinfo:
            connection.exec_driver_sql(
                "INSERT INTO context_sources"
                " (id, context_id, source_type, path, status, created_at, updated_at)"
                " VALUES ('doppelt', ?, 'document', 'uni/finanzierung.pdf', 'active',"
                " '2026-09-29 10:00:00', '2026-09-29 10:00:00')",
                (context_id,),
            )
        # SQLite nennt bei UNIQUE die Spalten, nicht den Constraint-Namen; die
        # Zuordnung zu ``uq_context_sources_context_id_path`` belegt
        # ``test_migration_adds_the_documented_constraints``.
        assert str(excinfo.value.orig) == (
            "UNIQUE constraint failed: context_sources.context_id, context_sources.path"
        )

    with connect(settings).connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM context_sources WHERE path = 'uni/finanzierung.pdf'"
            ).scalar_one()
            == 1
        )


def test_downgrade_and_reupgrade_preserve_core_data(tmp_path):
    """Downgrade auf 0007 und erneutes Upgrade duerfen den Altbestand nicht verlieren."""
    settings = make_settings(tmp_path, "downgrade.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0007_organiser_calendar_sync")
    session_factory = create_session_factory(settings)
    legacy = create_legacy_data(session_factory)
    before = read_legacy_data(session_factory, legacy)

    command.upgrade(config, "head")
    command.downgrade(config, "0007_organiser_calendar_sync")

    with connect(settings).connect() as connection:
        assert not table_names(connection) & {"contexts", "context_sources"}
        assert {"projects", "tasks", "events"} <= table_names(connection)
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []

    command.upgrade(config, "head")

    with connect(settings).connect() as connection:
        assert {"contexts", "context_sources"} <= table_names(connection)
        assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []

    assert read_legacy_data(session_factory, legacy) == before
    assert before["event_ids"] == legacy["event_ids"]


def test_upgrading_to_head_twice_is_repeatable(tmp_path):
    """Wiederholbarkeit: ein zweites ``upgrade head`` ist ein No-op.

    Dieser Test prueft ausdruecklich *nicht* den Uebergang 0007->0008; den trägt
    ``test_migration_from_0007_creates_context_tables_and_keeps_legacy_data`` und
    ``test_downgrade_and_reupgrade_preserve_core_data``.
    """
    settings = make_settings(tmp_path, "upgrade.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        project = create_project(database_session, title="Hausarbeit")
        task = create_task(database_session, title="Gliederung", project_id=project.id)
        project_id, task_id = project.id, task.id

    upgrade_database(settings)
    upgrade_database(settings)

    with connect(settings).connect() as connection:
        assert connection.exec_driver_sql(
            "SELECT version_num FROM alembic_version"
        ).scalar_one() == (ScriptDirectory.from_config(alembic_config(settings)).get_current_head())
        # Der Unique-Index existiert genau einmal, nicht einmal pro Lauf.
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = 'index' AND name = 'uq_contexts_knowledge_path'"
            ).scalar_one()
            == 1
        )
    with session_factory() as database_session:
        assert database_session.get(Context, project_id) is None
        assert get_project(database_session, project_id).title == "Hausarbeit"
        assert get_task(database_session, task_id).title == "Gliederung"
