"""Shared fixtures and builders for test_context_knowledge_lifecycle_regressions contracts; no executable tests."""

from __future__ import annotations

from pathlib import Path
import pytest
from conftest import make_settings
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.migrations import upgrade_database
from organiser.db.models import Context, Event, Project
from organiser.db.session import create_session_factory
from organiser.services.context_knowledge import (
    ContextKnowledgeService,
)
from organiser.services.domain import (
    create_context,
    list_events_for_entity,
)

MANUAL_PATH = "Bewerbungen/SAP 2026"

# -- Fixtures -----------------------------------------------------------------


@pytest.fixture
def session(tmp_path):
    """Eigene temporaere Datenbank pro Test; nichts wird in fremde Zustandshaltung geteilt."""
    settings = make_settings(tmp_path, "context-knowledge-lifecycle.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def service(session, knowledge_store) -> ContextKnowledgeService:
    return ContextKnowledgeService(session, knowledge_store)


# -- Hilfen -------------------------------------------------------------------


def knowledge_events(session: Session, context_id: str) -> list[Event]:
    """Nur die Audit-Ereignisse des Context-Knowledge-Service."""
    return [
        event
        for event in list_events_for_entity(session, entity_type="context", entity_id=context_id)
        if event.event_type.startswith("CONTEXT_KNOWLEDGE")
    ]


def event_types(session: Session, context_id: str) -> list[str]:
    return [event.event_type for event in knowledge_events(session, context_id)]


def tree(root: Path) -> set[str]:
    return {str(item.relative_to(root)) for item in root.rglob("*")}


def stored_path(session: Session, context_id: str) -> str | None:
    """Liest die Bindung ueber eine eigene Verbindung, also ohne Identity Map."""
    with session.get_bind().connect() as connection:
        return connection.execute(
            select(Context.knowledge_path).where(Context.id == context_id)
        ).scalar_one()


def stored_project_title(session: Session, project_id: str) -> str | None:
    with session.get_bind().connect() as connection:
        return connection.execute(
            select(Project.title).where(Project.id == project_id)
        ).scalar_one_or_none()


def make_project(session: Session, *, title: str, commit: bool = True) -> Project:
    """Fremde Arbeit, die je nach ``commit`` eine offene Transaktion hinterlaesst."""
    project = Project(title=title, status="active")
    session.add(project)
    session.flush()
    if commit:
        session.commit()
    return project


def raise_filesystem(*args, **kwargs):
    raise PermissionError(13, "Permission denied")


def sibling_session(session: Session) -> Session:
    """Zweite Session mit denselben Session-Einstellungen wie die Anwendung.

    ``expire_on_commit=False`` ist wichtig: nach ``commit()`` bleibt der
    gecachte Objektzustand stehen, ohne dass die Transaktion wieder aufgeht.
    """
    return Session(session.get_bind(), autoflush=False, expire_on_commit=False)


def stale_session(
    session: Session, context_id: str, store
) -> tuple[Session, Context, ContextKnowledgeService]:
    """Zweite Session mit einem veralteten, ungebundenen Context im Speicher.

    ``commit()`` beendet die Transaktion, ohne die gecachten Attributwerte zu
    verwerfen (``expire_on_commit=False``). Genau so sieht ein Prozess aus, der
    einen Context laenge haelt und spaeter schreibt.
    """
    stale = sibling_session(session)
    record = stale.get(Context, context_id)
    stale.commit()
    assert stale.in_transaction() is False
    assert record.knowledge_path is None
    return stale, record, ContextKnowledgeService(stale, store)


@pytest.fixture
def other_session(session):
    """Zweite Session auf derselben Datenbank, ohne gemeinsame Transaktion."""
    second = sibling_session(session)
    try:
        yield second
    finally:
        second.rollback()
        second.close()


@pytest.fixture
def context_with_knowledge(session, service) -> Context:
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    return created
