"""Api provenance contracts for topics."""

from __future__ import annotations

import pytest
from conftest import auth_headers
from organiser.services.domain import list_events_for_entity
from support.workspace_topics_tasks_api import (
    harness as harness,
    ROUTES as ROUTES,
    topic_file_for as topic_file_for,
)


# -- review follow-up: provenance on both ensure routes (F6) -------------------


@pytest.mark.parametrize("operation", ["context", "topic"])
def test_ensure_without_a_body_is_unchanged_and_audits_no_provenance(harness, operation):
    """Compatibility: no body means exactly what it meant before."""
    client, ids, factory, _, _ = harness
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )
    entity_type, entity_id = (
        ("context", ids["context"]) if operation == "context" else ("topic", ids["topic"])
    )

    assert client.post(path, headers=auth_headers()).status_code == 200

    with factory() as session:
        events = list_events_for_entity(session, entity_type=entity_type, entity_id=entity_id)
    knowledge_events = [event for event in events if "KNOWLEDGE" in event.event_type]
    assert knowledge_events
    assert all(event.source_id is None and event.source_type is None for event in knowledge_events)


@pytest.mark.parametrize("operation", ["context", "topic"])
def test_ensure_accepts_optional_provenance_and_attributes_the_audit(harness, operation):
    """``source_id = proposal.id`` must be attributable, for E and for operators."""
    client, ids, factory, _, _ = harness
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )
    body = {"source_type": "context_action_proposal", "source_id": "proposal-42"}

    response = client.post(path, json=body, headers=auth_headers())

    assert response.status_code == 200
    entity_type, entity_id = (
        ("context", ids["context"]) if operation == "context" else ("topic", ids["topic"])
    )
    with factory() as session:
        # Only this operation's own events; the fixture created the entity earlier.
        events = [
            event
            for event in list_events_for_entity(
                session, entity_type=entity_type, entity_id=entity_id
            )
            if "KNOWLEDGE" in event.event_type
        ]
    assert events
    assert all(event.source_id == "proposal-42" for event in events)
    assert all(event.source_type == "context_action_proposal" for event in events)


def test_topic_ensure_provenance_also_reaches_the_delegated_context_events(harness):
    """The topic ensure delegates to the context ensure; both must stay attributable."""
    client, ids, factory, _, _ = harness

    assert (
        client.post(
            f"/topics/{ids['topic']}/knowledge/ensure",
            json={"source_id": "proposal-7"},
            headers=auth_headers(),
        ).status_code
        == 200
    )

    with factory() as session:
        topic_events = [
            event
            for event in list_events_for_entity(
                session, entity_type="topic", entity_id=ids["topic"]
            )
            if "KNOWLEDGE" in event.event_type
        ]
        context_events = [
            event
            for event in list_events_for_entity(
                session, entity_type="context", entity_id=ids["context"]
            )
            if "KNOWLEDGE" in event.event_type
        ]
    assert topic_events and all(event.source_id == "proposal-7" for event in topic_events)
    assert context_events and all(event.source_id == "proposal-7" for event in context_events)
