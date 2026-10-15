"""Path preflight contracts for migrations."""

from __future__ import annotations

import ast
import importlib.util
from datetime import UTC, datetime
from pathlib import Path
import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from conftest import make_settings
from organiser.db.migrations import PROJECT_ROOT
from organiser.db.models import ContextSource, Event, Inbox, ProcessingRun
from organiser.db.session import create_session_factory
from organiser.domain.knowledge_path import KnowledgePathError, validate_context_path
from organiser.services.domain import (
    EventContext,
    add_context_source,
    create_context,
    create_project,
    update_context,
)
from support.paths import REPOSITORY_ROOT
from support.context_knowledge_migration_gate import (
    MIGRATION_0009 as MIGRATION_0009,
    REVISION_0007 as REVISION_0007,
    REVISION_0008 as REVISION_0008,
    REVISION_0009 as REVISION_0009,
    LEGACY_TABLES as LEGACY_TABLES,
    STAMP as STAMP,
    load_revision_0009 as load_revision_0009,
    alembic_config as alembic_config,
    enforced_engine as enforced_engine,
    revision_of as revision_of,
    table_names as table_names,
    snapshot as snapshot,
    foreign_key_violations as foreign_key_violations,
    rows as rows,
    index_sql as index_sql,
    insert_context as insert_context,
    insert_source as insert_source,
    create_legacy_world as create_legacy_world,
    assert_no_context_data as assert_no_context_data,
    assert_clean_integrity as assert_clean_integrity,
    assert_unchanged_at_0008 as assert_unchanged_at_0008,
    PATH_MATRIX as PATH_MATRIX,
)


@pytest.mark.parametrize(
    "bad_path",
    [
        "C:relative",
        "c:relative",
        "C:",
        "c:",
        "C:/pfad",
        "/absolut/pfad",
        "//server/share",
        "bewerbungen\\sap",
        "a//b",
        "a/./b",
        "../aussen",
        "a/b/",
        "a\x00b",
        "a\nb",
        "-segment",
        "topics",
        "bewerbungen/notes.md",
        "bewerbungen/Overview.md",
        " " + "a" * 512,
        "a" * 513,
    ],
)
def test_preflight_aborts_on_invalid_legacy_path_without_any_change(tmp_path, bad_path):
    """Ungueltiger Altpfad: Abbruch, und die Datenbank bleibt exakt auf 0008."""
    settings = make_settings(tmp_path, "preflight-invalid.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0008)

    insert_context(settings, "gut", "bewerbungen/sap")
    insert_context(settings, "kaputt", bad_path)
    insert_context(settings, "ungebunden", None)
    expected_contexts = [
        ("gut", "bewerbungen/sap"),
        ("kaputt", bad_path),
        ("ungebunden", None),
    ]
    before_events = []
    before_snapshot = snapshot(settings, ("contexts", "context_sources"))

    with pytest.raises(RuntimeError) as failure:
        command.upgrade(config, REVISION_0009)

    message = str(failure.value)
    assert "Migration 0009 wird abgebrochen; die Datenbank wurde nicht veraendert." in message
    assert "kaputt" in message
    assert f"knowledge_path={bad_path!r}" in message
    assert "Bereinigung manuell" in message
    # Der Gute bleibt unbeanstandet; nur der wirklich falsche Wert wird genannt.
    assert "Ungueltige bestehende knowledge_path-Werte" in message

    assert_unchanged_at_0008(settings, expected_contexts, before_events)
    # Auch die unveraenderten Spalten (status, parent, created_at) sind unangetastet.
    assert snapshot(settings, ("contexts", "context_sources")) == before_snapshot
    assert_clean_integrity(settings)

    # Nach der dokumentierten manuellen Bereinigung laesst sich die Migration
    # ohne weiteres Zutun ausfuehren.
    with enforced_engine(settings).begin() as connection:
        connection.execute(text("UPDATE contexts SET knowledge_path = NULL WHERE id = 'kaputt'"))
    command.upgrade(config, REVISION_0009)
    assert revision_of(settings) == REVISION_0009
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None
    assert rows(settings, "SELECT id, knowledge_path FROM contexts ORDER BY id") == [
        ("gut", "bewerbungen/sap"),
        ("kaputt", None),
        ("ungebunden", None),
    ]


def test_preflight_aborts_on_duplicate_legacy_paths_and_names_every_context(tmp_path):
    settings = make_settings(tmp_path, "preflight-duplicate.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0008)

    insert_context(settings, "erster", "bewerbungen/sap")
    insert_context(settings, "zweiter", "bewerbungen/sap")
    insert_context(settings, "dritter", "bewerbungen/sap")
    insert_context(settings, "eigen", "finanzen/steuern")
    insert_context(settings, "ungebunden", None)
    before_contexts = rows(settings, "SELECT id, knowledge_path FROM contexts ORDER BY id")
    before_snapshot = snapshot(settings, ("contexts", "context_sources"))

    with pytest.raises(RuntimeError) as failure:
        command.upgrade(config, REVISION_0009)

    message = str(failure.value)
    assert "Mehrfach vergebene bestehende knowledge_path-Werte" in message
    # Der Preflight nennt alle betroffenen IDs in Abfrage-Reihenfolge (ORDER BY id),
    # damit der Operator genau die genannten Zeilen bereinigen kann.
    assert "dritter, erster, zweiter" in message
    # Der eigentliche Konfliktwert und der unauffaellige Nachbar werden genannt.
    assert "knowledge_path='bewerbungen/sap'" in message
    assert "Migration 0009 wird abgebrochen" in message
    assert "Ungueltige bestehende knowledge_path-Werte" not in message

    assert_unchanged_at_0008(settings, before_contexts, [])
    assert snapshot(settings, ("contexts", "context_sources")) == before_snapshot
    assert_clean_integrity(settings)

    # Nach der Bereinigung Variante B aus der Architekturdoku -- zwei der drei
    # Konfliktzellen loesen den Pfad, damit ensure_context_knowledge neu binden kann --
    # laesst sich 0009 ausfuehren.
    with enforced_engine(settings).begin() as connection:
        connection.execute(
            text("UPDATE contexts SET knowledge_path = NULL WHERE id IN ('zweiter', 'dritter')")
        )
    command.upgrade(config, REVISION_0009)

    assert revision_of(settings) == REVISION_0009
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None
    assert rows(settings, "SELECT id, knowledge_path FROM contexts ORDER BY id") == [
        ("dritter", None),
        ("eigen", "finanzen/steuern"),
        ("erster", "bewerbungen/sap"),
        ("ungebunden", None),
        ("zweiter", None),
    ]
    assert_clean_integrity(settings)


def test_preflight_accepts_many_unbound_contexts(tmp_path):
    """NULL heisst "noch nicht gebunden" und blockiert die Migration nicht."""
    settings = make_settings(tmp_path, "preflight-null.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0008)

    for index in range(5):
        insert_context(settings, f"offen-{index}", None)
    insert_context(settings, "gebunden-1", "bewerbungen/sap")
    insert_context(settings, "gebunden-2", "Finanzen/Steuern 2026")

    command.upgrade(config, REVISION_0009)

    assert revision_of(settings) == REVISION_0009
    assert rows(settings, "SELECT count(*) FROM contexts WHERE knowledge_path IS NULL") == [(5,)]
    assert rows(settings, "SELECT count(*) FROM contexts WHERE knowledge_path IS NOT NULL") == [(2,)]
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None
    assert_clean_integrity(settings)


def test_a_database_already_on_0009_does_not_rerun_the_preflight(tmp_path):
    """Der Preflight laeuft genau einmal; stehende 0009-Datenbanken bleiben unberuehrt.

    Alembic fuehrt eine Revision genau einmal aus. Eine Datenbank, die bereits auf
    0009 steht, fuehrt den Preflight also nicht erneut und wird weder geprueft noch
    zurueckgestuft. Der Test haelt diese Zusage fest, damit ein spaeterer Umbau
    der Revision sie nicht still aufhebt.
    """
    settings = make_settings(tmp_path, "already-on-0009.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)
    insert_context(settings, "vorhanden", "bewerbungen/sap")
    before = snapshot(settings, ("contexts", "context_sources"))

    # Ein Wert, den der Preflight zurueckweisen wuerde, entsteht danach per
    # direktem INSERT. Der Unique-Index greift dafuer nicht; er sichert nur Eindeutigkeit.
    insert_context(settings, "spaeter_ungueltig", "C:relative")

    command.upgrade(config, REVISION_0009)

    assert revision_of(settings) == REVISION_0009
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None
    after = snapshot(settings, ("contexts", "context_sources"))
    # Die vorher bestehende Zeile ist unveraendert; nur der spaetere kam hinzu.
    assert after["context_sources"] == before["context_sources"]
    assert [row for row in after["contexts"] if row[0] == "vorhanden"] == before["contexts"]
    assert [row for row in after["contexts"] if row[0] == "spaeter_ungueltig"] == [
        ("spaeter_ungueltig", "Titel", None, "topic", "active", None, "C:relative", STAMP, STAMP)
    ]
