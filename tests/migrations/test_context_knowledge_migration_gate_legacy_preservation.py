"""Legacy preservation contracts for migrations."""

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
# Echte Migrationen ueber den vollen Versionspfad
# --------------------------------------------------------------------------------------


def test_full_chain_0007_to_0009_preserves_every_legacy_row(tmp_path):
    """0007 -> 0008 -> 0009: kein Altbestand und kein Event verliert ein Byte."""
    settings = make_settings(tmp_path, "chain-0007-0009.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0007)
    session_factory = create_session_factory(settings)

    assert_no_context_data(settings)
    legacy = create_legacy_world(session_factory)
    before = snapshot(settings, LEGACY_TABLES)

    # Der Altbestand ist nicht leer; sonst waere der Vergleich nachher aussagelos.
    assert len(before["projects"]) == 1
    assert len(before["tasks"]) == 2
    assert len(before["processing_runs"]) == 1
    assert len(before["events"]) >= 5
    # Spaltenfolge der events-Tabelle: id, occurred_at, event_type, entity_type,
    # entity_id, source_type, source_id, correlation_id, causation_event_id, payload.
    assert any(row[9] is not None for row in before["events"]), "Event-Payloads muessen gefuellt sein"
    assert any(row[6] is not None for row in before["events"]), "Event-Metadaten muessen gesetzt sein"
    assert any(row[7] is not None for row in before["events"]), "correlation_id muessen belegt sein"
    assert any(row[8] is not None for row in before["events"]), "causation_event_id muessen belegt sein"

    command.upgrade(config, REVISION_0008)
    assert {"contexts", "context_sources"} <= table_names(settings)
    assert revision_of(settings) == REVISION_0008
    # 0008 hat den Altbestand unangetastet gelassen.
    assert snapshot(settings, LEGACY_TABLES) == before

    with session_factory() as session:
        root = create_context(session, title="Bewerbungen", type="topic")
        bound = create_context(
            session, title="SAP", type="application", parent_context_id=root.id, knowledge_path="bewerbungen/sap"
        )
        unbound = create_context(session, title="Vorlesungen", type="topic")
        create_context(session, title="Schwarz Gruppe", type="application")
        update_context(session, context_id=unbound.id, description="Noch ohne Bindung")
        add_context_source(
            session, context_id=root.id, source_type="document", path="uni/finanzierung.pdf"
        )
        add_context_source(session, context_id=root.id, source_type="folder", path="uni/vorlesungen")
        add_context_source(session, context_id=bound.id, source_type="link", path="https://example.org")
        context_ids = {
            "root": root.id,
            "bound": bound.id,
            "unbound": unbound.id,
            "sources": [
                source.id
                for source in session.query(ContextSource).order_by(ContextSource.id).all()
            ],
        }

    after_0008_context = snapshot(settings, ("contexts", "context_sources"))
    assert len(after_0008_context["contexts"]) == 4
    assert len(after_0008_context["context_sources"]) == 3
    assert sum(1 for row in after_0008_context["contexts"] if row[6] is None) == 3

    command.upgrade(config, REVISION_0009)

    assert revision_of(settings) == REVISION_0009
    assert len(ScriptDirectory.from_config(config).get_heads()) == 1
    assert_clean_integrity(settings)
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None

    # Jede Zeile des Altbestands inklusive Payloads und Metadaten ist unveraendert.
    # Die Context-Events aus 0008 sind zusaetzlich entstanden, nicht umgeschrieben.
    after = snapshot(settings, LEGACY_TABLES)
    assert {table: after[table][: len(before[table])] for table in LEGACY_TABLES} == before
    assert len(after["events"]) == len(before["events"]) + 8
    # 0009 fasst die bestehenden Context- und Source-Zeilen nicht an.
    assert snapshot(settings, ("contexts", "context_sources")) == after_0008_context

    assert len(rows(settings, "SELECT id FROM contexts")) == 4
    assert rows(settings, "SELECT knowledge_path FROM contexts WHERE id = :id", id=context_ids["bound"]) == [
        ("bewerbungen/sap",)
    ]
    assert rows(settings, "SELECT knowledge_path FROM contexts WHERE id = :id", id=context_ids["unbound"]) == [(None,)]
    assert rows(settings, "SELECT parent_context_id FROM contexts WHERE id = :id", id=context_ids["bound"]) == [
        (context_ids["root"],)
    ]
    assert [row[0] for row in rows(settings, "SELECT id FROM context_sources ORDER BY id")] == sorted(
        context_ids["sources"]
    )


def test_0009_downgrade_and_reupgrade_keep_real_context_and_source_rows(tmp_path):
    """0009 -> 0008 -> 0009 mit befuellten Context- und Source-Tabellen ohne Verlust."""
    settings = make_settings(tmp_path, "downgrade-0009.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, REVISION_0008)
    session_factory = create_session_factory(settings)

    with session_factory() as session:
        first = create_context(session, title="Bewerbungen", type="topic", knowledge_path="bewerbungen/sap")
        second = create_context(
            session, title="SAP", type="application", parent_context_id=first.id, knowledge_path="finanzen/steuern"
        )
        third = create_context(session, title="Vorlesungen", type="topic")
        fourth = create_context(session, title="Schwarz Gruppe", type="application")
        first_source = add_context_source(
            session, context_id=first.id, source_type="document", path="uni/alt.pdf"
        )
        second_source = add_context_source(
            session, context_id=first.id, source_type="note", path="notizen/heute.md"
        )
        third_source = add_context_source(
            session, context_id=second.id, source_type="folder", path="uni/vorlesungen"
        )
        ids = {
            "first": first.id,
            "second": second.id,
            "third": third.id,
            "fourth": fourth.id,
            "quelle-1": first_source.id,
            "quelle-2": second_source.id,
            "quelle-3": third_source.id,
        }

    command.upgrade(config, REVISION_0009)
    before = snapshot(settings, ("contexts", "context_sources"))
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None

    command.downgrade(config, REVISION_0008)

    assert revision_of(settings) == REVISION_0008
    # Der Downgrade entfernt nur den Index; Contexts und Sources bleiben unangetastet.
    assert index_sql(settings, "uq_contexts_knowledge_path") is None
    assert snapshot(settings, ("contexts", "context_sources")) == before
    assert_clean_integrity(settings)

    command.upgrade(config, REVISION_0009)

    assert revision_of(settings) == REVISION_0009
    assert index_sql(settings, "uq_contexts_knowledge_path") is not None
    assert snapshot(settings, ("contexts", "context_sources")) == before
    assert_clean_integrity(settings)
    # Nach knowledge_path, weil die IDs UUIDs sind und ihre Sortierung nichts bedeutet.
    assert rows(settings, "SELECT id, title, parent_context_id, knowledge_path FROM contexts ORDER BY id") == sorted(
        [
            (ids["first"], "Bewerbungen", None, "bewerbungen/sap"),
            (ids["second"], "SAP", ids["first"], "finanzen/steuern"),
            (ids["fourth"], "Schwarz Gruppe", None, None),
            (ids["third"], "Vorlesungen", None, None),
        ]
    )
    assert rows(
        settings,
        "SELECT s.id, s.context_id, s.path, s.source_type FROM context_sources s"
        " JOIN contexts c ON c.id = s.context_id ORDER BY s.path",
    ) == [
        (ids["quelle-2"], ids["first"], "notizen/heute.md", "note"),
        (ids["quelle-1"], ids["first"], "uni/alt.pdf", "document"),
        (ids["quelle-3"], ids["second"], "uni/vorlesungen", "folder"),
    ]
