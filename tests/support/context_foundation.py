"""Shared fixtures and builders for test_context_foundation contracts; no executable tests."""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.migrations import PROJECT_ROOT, upgrade_database
from organiser.db.models import Event, Project
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    create_project,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "context-foundation.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def without_timestamps(payload: dict) -> dict:
    """SQLite liefert Zeitstempel naiv zurueck; Vergleiche sollen die Werte selbst pruefen."""
    return {key: value for key, value in payload.items() if key not in {"created_at", "updated_at"}}


def alembic_config(settings) -> Config:
    """Eigener Config-Aufbau, damit der Test gezielt auf eine Revision migriert."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def connect(settings):
    return create_engine(settings.database_url)


def table_names(connection) -> set[str]:
    return {
        row[0]
        for row in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def create_legacy_data(session_factory) -> dict[str, object]:
    """Revision-feste 0007-Fixture; aktuelles Task-ORM braucht neuere Spalten."""
    with session_factory() as database_session:
        project = create_project(database_session, title="Hausarbeit")
        task_id = "legacy-task"
        database_session.execute(
            text(
                "INSERT INTO tasks (id,title,description,status,project_id,created_at,updated_at) "
                "VALUES (:id,'Gliederung','Kapitel 1','open',:project_id,:stamp,:stamp)"
            ),
            {"id": task_id, "project_id": project.id, "stamp": "2026-09-29 10:00:00"},
        )
        database_session.add_all(
            [
                Event(
                    event_type="TASK_CREATED",
                    entity_type="task",
                    entity_id=task_id,
                    payload={"title": "Gliederung"},
                ),
                Event(
                    event_type="TASK_UPDATED",
                    entity_type="task",
                    entity_id=task_id,
                    payload={"changes": {"description": {"old": None, "new": "Kapitel 1"}}},
                ),
            ]
        )
        database_session.commit()
        event_ids = [
            event.id
            for event in database_session.scalars(
                select(Event).where(Event.entity_id.in_([project.id, task_id])).order_by(Event.id)
            )
        ]
        return {"project_id": project.id, "task_id": task_id, "event_ids": event_ids}


def read_legacy_data(session_factory, legacy: dict[str, object]) -> dict[str, object]:
    with session_factory() as database_session:
        project = database_session.get(Project, legacy["project_id"])
        task = database_session.execute(
            text("SELECT title, description, project_id FROM tasks WHERE id=:id"),
            {"id": legacy["task_id"]},
        ).one()
        event_ids = [
            event.id
            for event in database_session.scalars(
                select(Event)
                .where(Event.entity_id.in_([legacy["project_id"], legacy["task_id"]]))
                .order_by(Event.id)
            )
        ]
        return {
            "project_title": project.title,
            "project_status": project.status,
            "task_title": task.title,
            "task_description": task.description,
            "task_project_id": task.project_id,
            "event_ids": event_ids,
        }


@pytest.fixture
def client(tmp_path):
    settings = make_settings(tmp_path, "context-api.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as test_client:
        yield test_client
