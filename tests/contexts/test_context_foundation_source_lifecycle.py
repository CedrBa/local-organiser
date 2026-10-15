"""Source lifecycle contracts for contexts."""

from __future__ import annotations

import pytest
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    add_context_source,
    create_context,
    get_context,
    get_context_source,
    list_child_contexts,
    list_context_sources,
    list_events_for_entity,
    remove_context_source,
    update_context,
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


def test_source_status_and_type_validation(session):
    created = create_context(session, title="Bewerbungen", type="topic")

    with pytest.raises(DomainValidationError):
        add_context_source(
            session, context_id=created.id, source_type="scanner", path="uni/vorlesungen"
        )
    with pytest.raises(DomainValidationError):
        add_context_source(session, context_id=created.id, source_type="document", path="  ")
    with pytest.raises(DomainValidationError):
        add_context_source(
            session,
            context_id=created.id,
            source_type="document",
            path="uni/vorlesungen",
            status="pending",
        )

    source = add_context_source(
        session, context_id=created.id, source_type="document", path="uni/vorlesungen"
    )
    assert (
        update_context_source(
            session, context_id=created.id, source_id=source.id, status="missing"
        ).status
        == "missing"
    )
    assert [
        item.id for item in list_context_sources(session, context_id=created.id, status="missing")
    ] == [source.id]
    assert list_context_sources(session, context_id=created.id, status="active") == []
    with pytest.raises(DomainValidationError):
        list_context_sources(session, context_id=created.id, status="unknown")


def test_source_update_and_removal(session):
    created = create_context(session, title="Bewerbungen", type="topic")
    source = add_context_source(
        session, context_id=created.id, source_type="document", path="uni/alt.pdf"
    )
    second = add_context_source(
        session, context_id=created.id, source_type="document", path="uni/neu.pdf"
    )

    unchanged = update_context_source(
        session, context_id=created.id, source_id=source.id, path="uni/alt.pdf"
    )
    assert unchanged.path == "uni/alt.pdf"
    assert [
        event.event_type
        for event in list_events_for_entity(
            session, entity_type="context_source", entity_id=source.id
        )
    ] == ["CONTEXT_SOURCE_ADDED"]

    update_context_source(
        session,
        context_id=created.id,
        source_id=source.id,
        path="uni/aktualisiert.pdf",
        source_type="note",
    )
    assert (
        get_context_source(session, context_id=created.id, source_id=source.id).path
        == "uni/aktualisiert.pdf"
    )
    assert (
        get_context_source(session, context_id=created.id, source_id=source.id).source_type
        == "note"
    )

    with pytest.raises(ConflictError):
        update_context_source(
            session, context_id=created.id, source_id=source.id, path="uni/neu.pdf"
        )
    with pytest.raises(DomainValidationError):
        update_context_source(
            session, context_id=created.id, source_id=source.id, source_type="scanner"
        )
    with pytest.raises(NotFoundError):
        update_context_source(session, context_id=created.id, source_id="fehlt", status="archived")

    remove_context_source(session, context_id=created.id, source_id=source.id)
    assert [item.id for item in list_context_sources(session, context_id=created.id)] == [second.id]
    assert [
        event.event_type
        for event in list_events_for_entity(
            session, entity_type="context_source", entity_id=source.id
        )
    ] == [
        "CONTEXT_SOURCE_ADDED",
        "CONTEXT_SOURCE_UPDATED",
        "CONTEXT_SOURCE_REMOVED",
    ]


def test_source_removal_does_not_remove_the_context(session):
    created = create_context(session, title="Finanzierung und Rechnungswesen", type="topic")
    source = add_context_source(
        session, context_id=created.id, source_type="document", path="uni/finanzierung.pdf"
    )

    remove_context_source(session, context_id=created.id, source_id=source.id)

    assert get_context(session, created.id).title == "Finanzierung und Rechnungswesen"
    assert list_context_sources(session, context_id=created.id) == []


def test_unknown_context_operations(session):
    for call in (
        lambda: get_context(session, "fehlt"),
        lambda: update_context(session, context_id="fehlt", title="Neu"),
        lambda: list_child_contexts(session, "fehlt"),
        lambda: get_context_source(session, context_id="fehlt", source_id="fehlt"),
        lambda: remove_context_source(session, context_id="fehlt", source_id="fehlt"),
    ):
        with pytest.raises(NotFoundError):
            call()
