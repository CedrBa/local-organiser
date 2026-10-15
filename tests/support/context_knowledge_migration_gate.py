"""Shared fixtures and builders for test_context_knowledge_migration_gate contracts; no executable tests."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from alembic.config import Config
from sqlalchemy import create_engine, event, text
from organiser.db.migrations import PROJECT_ROOT
from organiser.db.models import Event, Inbox, ProcessingRun
from organiser.services.domain import (
    EventContext,
    create_project,
)

MIGRATION_0009 = PROJECT_ROOT / "migrations" / "versions" / "0009_context_knowledge_path_unique.py"

REVISION_0007 = "0007_organiser_calendar_sync"

REVISION_0008 = "0008_context_foundation"

REVISION_0009 = "0009_context_knowledge_path_unique"

# Tabellen, die schon vor 0008 existieren. Ihr Inhalt ist der Altbestand, den
# 0008 und 0009 unveraendert durchreichen muessen.
LEGACY_TABLES = ("inbox", "projects", "tasks", "events", "processing_runs")

STAMP = "2026-09-29 10:00:00"


def load_revision_0009():
    """Laedt 0009 als eigenes Modul, ohne es in die Package-Struktur zu legen."""
    spec = importlib.util.spec_from_file_location("revision_0009_under_test", MIGRATION_0009)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alembic_config(settings) -> Config:
    """Eigener Config-Aufbau, damit der Test gezielt auf eine Revision migriert."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def enforced_engine(settings):
    """Engine mit aktivem ``foreign_keys``-Pragma und geprueftem Zustand.

    SQLite erzwingt Fremdschluessel nur, wenn das Pragma auf der *konkreten*
    Verbindung steht. Ohne diese Assertion wuerde ein fehlerhafter INSERT still
    durchgehen und erst spaeter beim ``foreign_key_check`` auffallen.
    """
    engine = create_engine(settings.database_url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys = ON")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    return engine


def revision_of(settings) -> str | None:
    with enforced_engine(settings).connect() as connection:
        return connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()


def table_names(settings) -> set[str]:
    with enforced_engine(settings).connect() as connection:
        return {
            row[0]
            for row in connection.exec_driver_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }


def snapshot(settings, tables) -> dict[str, list[tuple]]:
    """Alle Zeilen inklusive aller Spalten als einfache Python-Werte.

    Bewusst ueber ``exec_driver_sql`` statt ueber das ORM: so geht keine
    Typumwandlung des Models dazwischen, und ein Spaltenwechsel faellt sofort auf.
    """
    snapshot_data: dict[str, list[tuple]] = {}
    with enforced_engine(settings).connect() as connection:
        for table in tables:
            rows = connection.exec_driver_sql(f"SELECT * FROM {table}").fetchall()
            snapshot_data[table] = sorted(tuple(row) for row in rows)
    return snapshot_data


def foreign_key_violations(settings) -> list[tuple]:
    with enforced_engine(settings).connect() as connection:
        return connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()


def rows(settings, statement: str, **parameters) -> list[tuple]:
    with enforced_engine(settings).connect() as connection:
        return connection.execute(text(statement), parameters).fetchall()


def index_sql(settings, name: str) -> str | None:
    with enforced_engine(settings).connect() as connection:
        return connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :name", {"name": name}
        ).scalar_one_or_none()


def insert_context(settings, context_id: str, knowledge_path: str | None) -> None:
    """Direkter INSERT, um Altwerte zu erzeugen, die die Domain nicht schreiben wuerde."""
    with enforced_engine(settings).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO contexts (id, title, type, status, knowledge_path, created_at, updated_at)"
                " VALUES (:id, 'Titel', 'topic', 'active', :path, :stamp, :stamp)"
            ),
            {"id": context_id, "path": knowledge_path, "stamp": STAMP},
        )


def insert_source(settings, source_id: str, context_id: str, path: str) -> None:
    with enforced_engine(settings).begin() as connection:
        connection.execute(
            text(
                "INSERT INTO context_sources"
                " (id, context_id, source_type, path, status, created_at, updated_at)"
                " VALUES (:id, :context_id, 'document', :path, 'active', :stamp, :stamp)"
            ),
            {"id": source_id, "context_id": context_id, "path": path, "stamp": STAMP},
        )


def create_legacy_world(session_factory) -> dict[str, object]:
    """Repraesentativer Altbestand auf 0007: Inbox, Projekte, Tasks, Events.

    Projekte entstehen über die Domain. Tasks/Taskevents sind eingefrorene SQL-
    Fixtures, damit das heutige Task-ORM keine zukünftigen Spalten auf 0007 liest.
    """
    moment = datetime(2026, 9, 17, 10, 0, tzinfo=UTC)
    with session_factory() as session:
        session.add(
            Inbox(
                id="inbox-1",
                created_at=moment,
                received_at=moment,
                text="Gliederung fuer die Hausarbeit schicken",
                source="api",
                raw_payload={"subject": "Hausarbeit", "attachments": ["skript.pdf"]},
                processing_status="processed",
                processed_at=moment,
            )
        )
        session.commit()
        project = create_project(
            session,
            title="Hausarbeit",
            description="Finanzierung und Rechnungswesen",
            context=EventContext(
                source_type="api", source_id="aufruf-1", correlation_id="vorgang-1"
            ),
        )
        task_id, child_id = "legacy-task", "legacy-child"
        for identifier, title, parent, status, completed_at in (
            (task_id, "Gliederung", None, "done", moment),
            (child_id, "Kapitel 1 schreiben", task_id, "open", None),
        ):
            session.execute(
                text(
                    "INSERT INTO tasks (id,title,description,status,project_id,parent_task_id,source_inbox_id,"
                    "estimated_minutes,cognitive_load,created_at,updated_at,completed_at) "
                    "VALUES (:id,:title,'Kapitel 1',:status,:project,:parent,'inbox-1',90,'high',:stamp,:stamp,:completed)"
                ),
                {
                    "id": identifier,
                    "title": title,
                    "status": status,
                    "project": project.id,
                    "parent": parent,
                    "stamp": moment.strftime("%Y-%m-%d %H:%M:%S.%f"),
                    "completed": completed_at.strftime("%Y-%m-%d %H:%M:%S.%f")
                    if completed_at
                    else None,
                },
            )
        start_event = None
        for event_type, entity_id in (
            ("TASK_CREATED", task_id),
            ("TASK_CREATED", child_id),
            ("TASK_STARTED", task_id),
            ("TASK_COMPLETED", task_id),
        ):
            task_event = Event(
                occurred_at=moment,
                event_type=event_type,
                entity_type="task",
                entity_id=entity_id,
                source_type="api",
                source_id="aufruf-2" if event_type == "TASK_COMPLETED" else "aufruf-1",
                correlation_id="vorgang-1",
                causation_event_id=start_event.id if event_type == "TASK_COMPLETED" else None,
                payload={
                    "task_id": entity_id,
                    "title": "Gliederung",
                    "status": "done" if event_type == "TASK_COMPLETED" else "open",
                },
            )
            session.add(task_event)
            session.flush()
            if event_type == "TASK_STARTED":
                start_event = task_event
        completed = task_event
        session.add(
            ProcessingRun(
                id="run-1",
                run_type="inbox",
                status="completed",
                source_inbox_id="inbox-1",
                model="lokal",
                started_at=moment,
                finished_at=moment,
                context_event_max_id=completed.id,
                output_payload={"tasks": [task_id, child_id]},
            )
        )
        session.commit()
        return {"project_id": project.id, "task_id": task_id, "child_id": child_id}


def assert_no_context_data(settings) -> None:
    assert not table_names(settings) & {"contexts", "context_sources"}


def assert_clean_integrity(settings) -> None:
    assert foreign_key_violations(settings) == []


# --------------------------------------------------------------------------------------
# Preflight: Abbruch ohne Nebenwirkung
# --------------------------------------------------------------------------------------


def assert_unchanged_at_0008(
    settings, expected_contexts: list[tuple], before_events: list[tuple]
) -> None:
    """Nach einem Preflight-Abbruch: Revisionsstand, Index und Zeilen exakt wie vorher."""
    assert revision_of(settings) == REVISION_0008
    assert index_sql(settings, "uq_contexts_knowledge_path") is None
    assert (
        rows(settings, "SELECT id, knowledge_path FROM contexts ORDER BY id") == expected_contexts
    )
    assert rows(settings, "SELECT id, event_type FROM events ORDER BY id") == before_events


# --------------------------------------------------------------------------------------
# Gemeinsame Matrix: eingefrorener Preflight gegen Laufzeitvalidator
# --------------------------------------------------------------------------------------


PATH_MATRIX = [
    # gueltige Context-Pfade
    "bewerbungen/sap",
    "a",
    "a/b/c/d",
    "Bewerbungen/SAP 2026",
    "a-b_c.d",
    "contexts/1f0c3a2b-0000-4000-8000-000000000000",
    "a/" * 255 + "x",
    # absolute Pfade
    "/absolut/pfad",
    "//server/share",
    "C:/pfad",
    "c:/pfad",
    "C:\\pfad",
    "\\\\server\\share",
    "bewerbungen\\sap",
    # Windows-Laufwerkspfade ohne fuehrenden Slash
    "C:relative",
    "c:relative",
    "C:",
    "c:",
    "1:2",
    "a:b",
    "a/b:c",
    "ab:cd",
    # leere und wandernde Segmente
    "",
    "   ",
    "a//b",
    "a/./b",
    "./a",
    ".",
    "..",
    "a/../b",
    "../a",
    "a/b/",
    "a/ /b",
    " lead/pfad",
    "pfad/lead ",
    "a/ lead",
    # Steuerzeichen, Laenge, fuehrender Bindestrich
    "a\x00b",
    "a\nb",
    "a\tb",
    "a\x7fb",
    "a" * 513,
    "a/" * 256 + "x",
    "-segment",
    "a/-b",
    # reservierte Store-Segmente
    "topics",
    "bewerbungen/topics",
    "notes.md",
    "bewerbungen/Overview.md",
    "contexts/x/progress.md",
    "bewerbungen/PROGRESS.MD",
]
