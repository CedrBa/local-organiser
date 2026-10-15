"""Persistence contracts for contexts."""

from __future__ import annotations

from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Context, ContextSource
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    add_context_source,
    create_context,
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


def test_context_tables_persist_sources_independently(tmp_path):
    settings = make_settings(tmp_path, "persist.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)

    with session_factory() as database_session:
        created = create_context(
            database_session, title="Finanzierung und Rechnungswesen", type="topic"
        )
        add_context_source(
            database_session, context_id=created.id, source_type="folder", path="uni/vorlesungen"
        )
        context_id = created.id

    with session_factory() as database_session:
        stored_context = database_session.get(Context, context_id)
        sources = list(
            database_session.query(ContextSource).filter(ContextSource.context_id == context_id)
        )
        assert stored_context.title == "Finanzierung und Rechnungswesen"
        assert [source.path for source in sources] == ["uni/vorlesungen"]
