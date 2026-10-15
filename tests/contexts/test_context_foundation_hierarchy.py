"""Hierarchy contracts for contexts."""

from __future__ import annotations

import pytest
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
    create_context,
    get_context,
    list_child_contexts,
    list_contexts,
    list_events_for_entity,
    set_context_status,
    update_context,
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


def test_parent_and_child_contexts(session):
    applications = create_context(session, title="Bewerbungen", type="topic")
    sap = create_context(
        session, title="SAP", type="application", parent_context_id=applications.id
    )
    schwarz = create_context(
        session, title="Schwarz Gruppe", type="application", parent_context_id=applications.id
    )
    sap_draft = create_context(
        session, title="Anschreiben", type="document", parent_context_id=sap.id
    )

    assert [context.id for context in list_child_contexts(session, applications.id)] == [
        sap.id,
        schwarz.id,
    ]
    assert [context.id for context in list_child_contexts(session, sap.id)] == [sap_draft.id]
    assert [context.id for context in list_contexts(session, parent_context_id=sap.id)] == [
        sap_draft.id
    ]
    assert [context.id for context in list_contexts(session, roots_only=True)] == [applications.id]
    assert get_context(session, sap_draft.id).parent_context_id == sap.id
    assert list_child_contexts(session, schwarz.id) == []


def test_parent_filters_and_validation(session):
    applications = create_context(session, title="Bewerbungen", type="topic")
    create_context(session, title="SAP", type="application", parent_context_id=applications.id)

    assert [context.id for context in list_contexts(session, type="application")] != []
    assert [context.id for context in list_contexts(session, type="topic")] == [applications.id]
    with pytest.raises(NotFoundError):
        list_contexts(session, parent_context_id="fehlt")


def test_invalid_parent_is_rejected(session):
    applications = create_context(session, title="Bewerbungen", type="topic")
    sap = create_context(
        session, title="SAP", type="application", parent_context_id=applications.id
    )

    with pytest.raises(NotFoundError):
        create_context(session, title="Ungueltig", type="application", parent_context_id="fehlt")
    with pytest.raises(DomainValidationError):
        update_context(session, context_id=applications.id, parent_context_id=applications.id)
    with pytest.raises(DomainValidationError):
        update_context(session, context_id=applications.id, parent_context_id=sap.id)
    with pytest.raises(NotFoundError):
        update_context(session, context_id=applications.id, parent_context_id="fehlt")

    assert get_context(session, applications.id).parent_context_id is None
    assert get_context(session, sap.id).parent_context_id == applications.id


def test_reparenting_and_detaching_a_context(session):
    applications = create_context(session, title="Bewerbungen", type="topic")
    other = create_context(session, title="Vorlesungen", type="topic")
    sap = create_context(
        session, title="SAP", type="application", parent_context_id=applications.id
    )

    update_context(session, context_id=sap.id, parent_context_id=other.id)
    assert get_context(session, sap.id).parent_context_id == other.id

    update_context(session, context_id=sap.id, parent_context_id=None)
    assert get_context(session, sap.id).parent_context_id is None
    assert [context.id for context in list_contexts(session, roots_only=True)] == [
        applications.id,
        other.id,
        sap.id,
    ]


def test_context_update_records_only_real_changes(session):
    created = create_context(
        session, title="Bewerbungen", type="topic", context=EventContext(correlation_id="ctx")
    )

    same = update_context(session, context_id=created.id, title="Bewerbungen")
    assert same.updated_at == created.updated_at
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == ["CONTEXT_CREATED"]

    update_context(
        session,
        context_id=created.id,
        title="Bewerbungsunterlagen",
        description="SAP und Schwarz Gruppe",
    )
    events = list_events_for_entity(session, entity_type="context", entity_id=created.id)

    assert [event.event_type for event in events] == ["CONTEXT_CREATED", "CONTEXT_UPDATED"]
    assert events[1].payload["changes"]["title"] == {
        "old": "Bewerbungen",
        "new": "Bewerbungsunterlagen",
    }
    assert events[0].correlation_id == "ctx"


def test_context_status_transitions(session):
    created = create_context(session, title="Bewerbungen", type="topic")

    assert set_context_status(session, context_id=created.id, status="on_hold").status == "on_hold"
    assert set_context_status(session, context_id=created.id, status="active").status == "active"
    assert (
        set_context_status(session, context_id=created.id, status="cancelled").status == "cancelled"
    )
    assert set_context_status(session, context_id=created.id, status="active").status == "active"
    assert (
        set_context_status(session, context_id=created.id, status="completed").status == "completed"
    )

    with pytest.raises(ConflictError):
        set_context_status(session, context_id=created.id, status="cancelled")
    with pytest.raises(DomainValidationError):
        set_context_status(session, context_id=created.id, status="archived")
    with pytest.raises(NotFoundError):
        set_context_status(session, context_id="fehlt", status="on_hold")

    assert [context.id for context in list_contexts(session, status="completed")] == [created.id]
    with pytest.raises(DomainValidationError):
        list_contexts(session, status="archived")


def test_repeated_status_change_writes_no_event(session):
    created = create_context(session, title="Bewerbungen", type="topic")
    set_context_status(session, context_id=created.id, status="on_hold")

    set_context_status(session, context_id=created.id, status="on_hold")

    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == [
        "CONTEXT_CREATED",
        "CONTEXT_STATUS_CHANGED",
    ]
