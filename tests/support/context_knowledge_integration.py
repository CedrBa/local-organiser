"""Shared fixtures and builders for test_context_knowledge_integration contracts; no executable tests."""

from __future__ import annotations

from pathlib import Path
import pytest
from alembic.config import Config
from sqlalchemy import text
from conftest import make_settings
from organiser.db.migrations import PROJECT_ROOT, upgrade_database
from organiser.db.session import create_session_factory
from organiser.services.context_knowledge import (
    ContextKnowledgeService,
)
from organiser.services.domain import (
    list_events_for_entity,
)


def alembic_config(settings) -> Config:
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "context-knowledge.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def service(session, knowledge_store) -> ContextKnowledgeService:
    return ContextKnowledgeService(session, knowledge_store)


def event_types(session, context_id: str) -> list[str]:
    return [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=context_id)
    ]


def tree(root: Path) -> set[str]:
    return {str(item.relative_to(root)) for item in root.rglob("*")}


def _raise_commit() -> None:
    """Simuliert einen Datenbankausfall vor jeder Dateisystemmutation."""
    raise RuntimeError("Datenbank nicht erreichbar")


def _raise_filesystem(*args, **kwargs):
    """Simuliert einen Dateisystemausfall nach bereits erfolgter DB-Bindung."""
    raise PermissionError(13, "Permission denied")


# -- migration ----------------------------------------------------------------


def _insert_context(connection, context_id: str, knowledge_path: str | None) -> None:
    connection.execute(
        text(
            "INSERT INTO contexts (id, title, type, status, knowledge_path, created_at, updated_at) "
            "VALUES (:id, 'Titel', 'topic', 'active', :path, '2026-09-29 10:00:00', '2026-09-29 10:00:00')"
        ),
        {"id": context_id, "path": knowledge_path},
    )
