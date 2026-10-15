"""Constraints contracts for migrations."""

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


# --------------------------------------------------------------------------------------
# Constraints durch Verhalten
# --------------------------------------------------------------------------------------


def test_database_rejects_a_second_context_with_the_same_knowledge_path(tmp_path):
    """Nicht-NULL knowledge_path ist eindeutig -- in der Datenbank, nicht nur in der Domain."""
    settings = make_settings(tmp_path, "unique-knowledge-path.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)

    insert_context(settings, "erster", "bewerbungen/sap")
    insert_context(settings, "zweiter", None)

    engine = enforced_engine(settings)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            connection.execute(
                text(
                    "INSERT INTO contexts (id, title, type, status, knowledge_path, created_at, updated_at)"
                    " VALUES ('dritter', 'Titel', 'topic', 'active', 'bewerbungen/sap', :stamp, :stamp)"
                ),
                {"stamp": STAMP},
            )
    engine.dispose()

    assert "UNIQUE constraint failed: contexts.knowledge_path" in str(failure.value.orig)
    # Der fehlerhafte Versuch hat keine Zeile hinterlassen.
    assert rows(settings, "SELECT id FROM contexts ORDER BY id") == [("erster",), ("zweiter",)]
    assert_clean_integrity(settings)


def test_database_allows_several_unbound_contexts_and_their_later_binding(tmp_path):
    """NULL bedeutet "noch nicht gebunden": beliebig viele, danach je ein Pfad."""
    settings = make_settings(tmp_path, "many-null.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)

    for index in range(6):
        insert_context(settings, f"offen-{index}", None)

    engine = enforced_engine(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE contexts SET knowledge_path = 'contexts/offen-0' WHERE id = 'offen-0'"
            )
        )
        connection.execute(
            text(
                "UPDATE contexts SET knowledge_path = 'contexts/offen-1' WHERE id = 'offen-1'"
            )
        )
        with pytest.raises(IntegrityError) as failure:
            connection.execute(
                text("UPDATE contexts SET knowledge_path = 'contexts/offen-0' WHERE id = 'offen-2'")
            )
    engine.dispose()

    assert "UNIQUE constraint failed: contexts.knowledge_path" in str(failure.value.orig)
    assert rows(settings, "SELECT id, knowledge_path FROM contexts ORDER BY id") == [
        ("offen-0", "contexts/offen-0"),
        ("offen-1", "contexts/offen-1"),
        ("offen-2", None),
        ("offen-3", None),
        ("offen-4", None),
        ("offen-5", None),
    ]


def test_database_rejects_duplicate_source_paths_only_within_the_same_context(tmp_path):
    settings = make_settings(tmp_path, "unique-source-path.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)

    insert_context(settings, "erster", None)
    insert_context(settings, "zweiter", None)
    insert_source(settings, "quelle-a", "erster", "uni/finanzierung.pdf")
    # Derselbe Pfad in einem anderen Context ist erlaubt.
    insert_source(settings, "quelle-b", "zweiter", "uni/finanzierung.pdf")

    engine = enforced_engine(settings)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            insert_source(settings, "quelle-c", "erster", "uni/finanzierung.pdf")
    engine.dispose()

    assert str(failure.value.orig) == (
        "UNIQUE constraint failed: context_sources.context_id, context_sources.path"
    )
    assert rows(settings, "SELECT id, context_id, path FROM context_sources ORDER BY id") == [
        ("quelle-a", "erster", "uni/finanzierung.pdf"),
        ("quelle-b", "zweiter", "uni/finanzierung.pdf"),
    ]


@pytest.mark.parametrize(
    "statement, expected",
    [
        (
            "INSERT INTO contexts (id, title, type, status, knowledge_path, created_at, updated_at)"
            " VALUES ('ungueltig', 'Titel', 'topic', 'archiviert', NULL, :stamp, :stamp)",
            "CHECK constraint failed: ck_contexts_status",
        ),
        (
            "INSERT INTO context_sources"
            " (id, context_id, source_type, path, status, created_at, updated_at)"
            " VALUES ('ungueltig', 'erster', 'scanner', 'uni/x', 'active', :stamp, :stamp)",
            "CHECK constraint failed: ck_context_sources_source_type",
        ),
        (
            "INSERT INTO context_sources"
            " (id, context_id, source_type, path, status, created_at, updated_at)"
            " VALUES ('ungueltig', 'erster', 'document', 'uni/x', 'pending', :stamp, :stamp)",
            "CHECK constraint failed: ck_context_sources_status",
        ),
    ],
)
def test_database_rejects_invalid_status_and_source_type_on_insert(tmp_path, statement, expected):
    """CHECK-Zusagen greifen beim INSERT, nicht erst bei einem spaeteren Check."""
    settings = make_settings(tmp_path, "check-constraints.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)
    insert_context(settings, "erster", None)

    engine = enforced_engine(settings)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            connection.execute(text(statement), {"stamp": STAMP})
    engine.dispose()

    assert str(failure.value.orig) == expected
    assert rows(settings, "SELECT id FROM contexts") == [("erster",)]
    assert rows(settings, "SELECT id FROM context_sources") == []
    assert_clean_integrity(settings)


def test_the_fk_pragma_is_really_enforced_on_the_connection_under_test(tmp_path):
    """Gegenprobe zur Aussagekraft der FK-Tests.

    ``PRAGMA foreign_key_check`` findet Verstoesse auch dann, wenn beim INSERT gar
    nicht durchgegriffen wurde. Dieser Test belegt, dass auf der Testverbindung ein
    fehlerhafter FK-INSERT tatsaechlich scheitert. Ohne diese Zusage waeren die
    FK-Assertions der Folgefaelle nicht aussagekraeftig.
    """
    settings = make_settings(tmp_path, "fk-enforcement-probe.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)
    insert_context(settings, "erster", None)

    enforced = enforced_engine(settings)
    with enforced.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            connection.execute(
                text(
                    "INSERT INTO context_sources"
                    " (id, context_id, source_type, path, status, created_at, updated_at)"
                    " VALUES ('fehlt', 'gibt-es-nicht', 'document', 'uni/x', 'active', :stamp, :stamp)"
                ),
                {"stamp": STAMP},
            )
    enforced.dispose()

    assert str(failure.value.orig) == "FOREIGN KEY constraint failed"

    # Derselbe Fehler faellt auch beim nachtraeglichen Check auf -- dort waere er
    # allein kein Beweis, dass die Datenbank den Fehler beim Schreiben verhindert.
    assert foreign_key_violations(settings) == []


@pytest.mark.parametrize(
    "statement, expected",
    [
        (
            "INSERT INTO contexts (id, title, type, status, parent_context_id, knowledge_path, created_at, updated_at)"
            " VALUES ('kind', 'Titel', 'topic', 'active', 'fehlt', NULL, :stamp, :stamp)",
            "FOREIGN KEY constraint failed",
        ),
        (
            "INSERT INTO context_sources"
            " (id, context_id, source_type, path, status, created_at, updated_at)"
            " VALUES ('quelle', 'fehlt', 'document', 'uni/x', 'active', :stamp, :stamp)",
            "FOREIGN KEY constraint failed",
        ),
    ],
)
def test_database_rejects_invalid_parent_and_source_foreign_keys_on_insert(tmp_path, statement, expected):
    """FK-Zusagen greifen beim INSERT; ein nachtraeglicher foreign_key_check waere zu spaet."""
    settings = make_settings(tmp_path, "fk-constraints.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)
    insert_context(settings, "erster", None)

    engine = enforced_engine(settings)
    with engine.begin() as connection:
        with pytest.raises(IntegrityError) as failure:
            connection.execute(text(statement), {"stamp": STAMP})
    engine.dispose()

    assert str(failure.value.orig) == expected
    assert rows(settings, "SELECT id FROM contexts") == [("erster",)]
    assert rows(settings, "SELECT id FROM context_sources") == []
    assert_clean_integrity(settings)


def test_database_allows_a_valid_parent_and_source_reference(tmp_path):
    """Gegenprobe: gueltige Referenzen bleiben moeglich, sonst waeren die Tests trivial."""
    settings = make_settings(tmp_path, "fk-allowed.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0009)

    insert_context(settings, "eltern", None)
    insert_source(settings, "quelle", "eltern", "uni/x")
    engine = enforced_engine(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO contexts (id, title, type, status, parent_context_id, knowledge_path, created_at, updated_at)"
                " VALUES ('kind', 'Titel', 'topic', 'active', 'eltern', 'a/b', :stamp, :stamp)"
            ),
            {"stamp": STAMP},
        )
    engine.dispose()

    assert rows(settings, "SELECT id, parent_context_id FROM contexts ORDER BY id") == [
        ("eltern", None),
        ("kind", "eltern"),
    ]
    assert rows(settings, "SELECT id, context_id FROM context_sources") == [("quelle", "eltern")]
    assert_clean_integrity(settings)
