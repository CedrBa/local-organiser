"""Coordination contracts for context_knowledge."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from conftest import make_settings
from organiser.services.domain import (
    create_context,
    list_contexts,
)
from support.context_knowledge_integration import (
    alembic_config as alembic_config,
    session as session,
    service as service,
    event_types as event_types,
    tree as tree,
    _raise_commit as _raise_commit,
    _raise_filesystem as _raise_filesystem,
    _insert_context as _insert_context,
)


def test_migration_0009_upgrades_a_database_with_existing_contexts(tmp_path):
    settings = make_settings(tmp_path, "migration-ok.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0008_context_foundation")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        _insert_context(connection, "a", None)
        _insert_context(connection, "b", "bewerbungen/sap")
        _insert_context(connection, "c", "finanzen/steuern")

    command.upgrade(config, "head")

    with engine.connect() as connection:
        index_rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = 'contexts'"
        ).fetchall()
        rows = connection.exec_driver_sql(
            "SELECT id, knowledge_path FROM contexts ORDER BY id"
        ).fetchall()
    engine.dispose()

    names = {row[0] for row in index_rows}
    assert "uq_contexts_knowledge_path" in names
    assert any(
        "UNIQUE" in (row[1] or "").upper()
        for row in index_rows
        if row[0] == "uq_contexts_knowledge_path"
    )
    assert rows == [("a", None), ("b", "bewerbungen/sap"), ("c", "finanzen/steuern")]


def test_migration_0009_keeps_multiple_unbound_contexts_possible(tmp_path):
    settings = make_settings(tmp_path, "migration-null.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0008_context_foundation")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        for index in range(3):
            _insert_context(connection, f"offen-{index}", None)

    command.upgrade(config, "head")

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM contexts WHERE knowledge_path IS NULL"
            ).scalar_one()
            == 3
        )
    engine.dispose()


@pytest.mark.parametrize(
    "bad_path",
    ["/absolut/pfad", "../aussen", "a//b", "contexts/x/topics", "a\x00b", "a" * 513],
)
def test_migration_0009_refuses_invalid_legacy_values(tmp_path, bad_path):
    settings = make_settings(tmp_path, "migration-invalid.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0008_context_foundation")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        _insert_context(connection, "kaputt", bad_path)

    with pytest.raises(RuntimeError) as failure:
        command.upgrade(config, "head")

    message = str(failure.value)
    assert "Migration 0009 wird abgebrochen" in message
    assert "kaputt" in message
    assert "Bereinigung manuell" in message
    with engine.connect() as connection:
        # Nichts wurde still umbenannt oder normalisiert.
        assert (
            connection.exec_driver_sql(
                "SELECT knowledge_path FROM contexts WHERE id = 'kaputt'"
            ).scalar_one()
            == bad_path
        )
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = 'index' AND name = 'uq_contexts_knowledge_path'"
            ).scalar_one()
            == 0
        )
    engine.dispose()


def test_migration_0009_refuses_duplicate_legacy_values_and_names_the_contexts(tmp_path):
    settings = make_settings(tmp_path, "migration-duplicate.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "0008_context_foundation")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        _insert_context(connection, "erster", "bewerbungen/sap")
        _insert_context(connection, "zweiter", "bewerbungen/sap")

    with pytest.raises(RuntimeError) as failure:
        command.upgrade(config, "head")

    message = str(failure.value)
    assert "Mehrfach vergebene" in message
    assert "erster" in message and "zweiter" in message
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM contexts WHERE knowledge_path = 'bewerbungen/sap'"
            ).scalar_one()
            == 2
        )
    engine.dispose()


def test_migration_0009_downgrade_removes_only_the_new_index(tmp_path):
    settings = make_settings(tmp_path, "migration-downgrade.sqlite3")
    config = alembic_config(settings)
    command.upgrade(config, "head")
    engine = create_engine(settings.database_url)

    command.downgrade(config, "0008_context_foundation")

    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type = 'index' AND name = 'uq_contexts_knowledge_path'"
            ).scalar_one()
            == 0
        )
        assert connection.exec_driver_sql("SELECT count(*) FROM contexts").scalar_one() == 0
    engine.dispose()


def test_database_rejects_a_second_binding_of_the_same_path(session):
    """Die Eindeutigkeit ist nicht nur eine Domainregel, sondern auch eine DB-Zusage."""
    create_context(session, title="Erster", type="topic", knowledge_path="geteilt/pfad")
    create_context(session, title="Zweiter", type="topic")
    created = list_contexts(session)[1]
    created.knowledge_path = "geteilt/pfad"

    with pytest.raises(IntegrityError) as failure:
        session.commit()
    assert "UNIQUE constraint failed" in str(failure.value)
    assert "contexts.knowledge_path" in str(failure.value)
    session.rollback()
