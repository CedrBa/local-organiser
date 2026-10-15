"""Sources contracts for contexts."""

from __future__ import annotations

import pytest
from organiser.services.domain import (
    ConflictError,
    NotFoundError,
    add_context_source,
    create_context,
    get_context_source,
    list_context_sources,
    list_events_for_entity,
    remove_context_source,
    update_context_source,
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


def test_context_accepts_multiple_sources(session):
    created = create_context(session, title="Bewerbungen", type="topic")
    lecture = add_context_source(
        session, context_id=created.id, source_type="document", path="uni/finanzierung.pdf"
    )
    note = add_context_source(
        session, context_id=created.id, source_type="note", path="notizen/finanzierung.md"
    )
    folder = add_context_source(
        session, context_id=created.id, source_type="folder", path="uni/vorlesungen"
    )

    sources = list_context_sources(session, context_id=created.id)

    assert [source.id for source in sources] == [lecture.id, note.id, folder.id]
    assert {source.source_type for source in sources} == {"document", "note", "folder"}
    assert all(source.status == "active" for source in sources)
    assert [
        event.event_type
        for event in list_events_for_entity(
            session, entity_type="context_source", entity_id=note.id
        )
    ] == ["CONTEXT_SOURCE_ADDED"]


def test_duplicate_source_in_same_context_conflicts_but_is_allowed_in_another(session):
    created = create_context(session, title="Bewerbungen", type="topic")
    other = create_context(session, title="Finanzierung", type="topic")
    add_context_source(
        session, context_id=created.id, source_type="document", path="uni/finanzierung.pdf"
    )
    add_context_source(
        session, context_id=other.id, source_type="document", path="uni/finanzierung.pdf"
    )

    with pytest.raises(ConflictError):
        add_context_source(
            session, context_id=created.id, source_type="document", path="uni/finanzierung.pdf"
        )


def test_source_belongs_to_its_context(session):
    created = create_context(session, title="Bewerbungen", type="topic")
    other = create_context(session, title="SAP", type="application")
    source = add_context_source(
        session, context_id=created.id, source_type="link", path="https://example.org"
    )

    assert get_context_source(session, context_id=created.id, source_id=source.id).id == source.id
    with pytest.raises(NotFoundError):
        get_context_source(session, context_id=other.id, source_id=source.id)
    with pytest.raises(NotFoundError):
        update_context_source(
            session, context_id=other.id, source_id=source.id, path="https://example.com"
        )
    with pytest.raises(NotFoundError):
        remove_context_source(session, context_id=other.id, source_id=source.id)
    assert [item.id for item in list_context_sources(session, context_id=created.id)] == [source.id]


def test_source_of_unknown_context_is_not_found(session):
    with pytest.raises(NotFoundError):
        add_context_source(
            session, context_id="fehlt", source_type="document", path="uni/finanzierung.pdf"
        )
    with pytest.raises(NotFoundError):
        list_context_sources(session, context_id="fehlt")
