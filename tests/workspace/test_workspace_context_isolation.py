"""Isolation contracts for workspace."""

from __future__ import annotations

import json
import pytest
from organiser.services.domain import create_project
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import (
    WorkspaceSelectionError,
    workspace_context,
)
from support.workspace import (
    HASH_C,
    add_context,
    add_open_task,
    add_resource,
    add_snapshot,
    add_source,
    add_topic,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env
from support.workspace_context import (
    build as build,
    labels as labels,
    hashlib_hex as hashlib_hex,
)


# ---------------------------------------------------------------------------
# Context isolation
# ---------------------------------------------------------------------------


def test_two_contexts_with_the_same_title_never_mix(finance):
    session, store = finance["session"], finance["store"]
    twin = add_context(session, title=finance["context"].title)
    twin_source = add_source(session, twin, path="/erlaubt/zwilling")
    twin_resource = add_resource(
        session, twin, title="Nur im Zwilling", source=twin_source, content_hash=HASH_C
    )
    add_snapshot(session, twin_resource, text="Geheimnis des Zwillings", content_hash=HASH_C)
    add_open_task(session, context=twin, title="Nur im Zwilling")

    original = build(finance)
    other = workspace_context(session, twin.id, store)

    assert twin_resource.id not in {resource.id for resource in original.resources}
    assert "Geheimnis des Zwillings" not in json.dumps(original.model_dump(mode="json"))
    assert [resource.id for resource in other.resources] == [twin_resource.id]
    assert [task.title for task in original.current_tasks] != ["Nur im Zwilling"]


def test_a_topic_id_from_another_context_is_refused(finance):
    session, store = finance["session"], finance["store"]
    other = add_context(session, title="Anderer Kurs")
    foreign_topic = add_topic(session, other, title="Fremdes Thema")

    with pytest.raises(WorkspaceSelectionError) as failure:
        workspace_context(session, finance["context"].id, store, topic_ids=[foreign_topic.id])

    assert foreign_topic.id in str(failure.value)
    assert "nicht zum angefragten Context" in str(failure.value)


def test_a_resource_id_from_another_context_is_refused(finance):
    session, store = finance["session"], finance["store"]
    other = add_context(session, title="Anderer Kurs")
    foreign = add_resource(session, other)

    with pytest.raises(WorkspaceSelectionError):
        workspace_context(session, finance["context"].id, store, resource_ids=[foreign.id])


def test_an_unknown_focus_id_is_refused_instead_of_guessed(finance):
    with pytest.raises(WorkspaceSelectionError) as failure:
        workspace_context(
            finance["session"], finance["context"].id, finance["store"], topic_ids=["gibt-es-nicht"]
        )

    assert "unbekannte ID" in str(failure.value)


def test_lookalike_topic_titles_do_not_resolve_across_contexts(finance):
    """A topic of another context with the *same* title stays out of this packet."""
    session = finance["session"]
    other = add_context(session, title="Anderer Kurs")
    add_topic(session, other, title="Bilanzierung")

    packet = build(finance)

    assert {topic.title for topic in packet.topics} == {"Bilanzierung", "Abschreibung"}
    assert packet.context.topic_count == 2


def test_a_project_never_produces_a_guessed_context(finance):
    session, store = finance["session"], finance["store"]
    project = create_project(session, title="Finanzierung und Rechnungswesen")
    unbound = add_open_task(session, title="Projektaufgabe ohne Kontext", project_id=project.id)

    packet = build(finance)
    from organiser.services.context import task_context

    task_packet = task_context(session, unbound.id, store=store)

    assert task_packet.workspace is None, "ein Project ist keine Context-Bindung"
    assert unbound.id not in {task.id for task in packet.current_tasks}


def test_child_contexts_and_other_life_areas_are_never_aggregated(finance):
    session = finance["session"]
    child = add_context(session, title="Kindbereich", parent=finance["context"].id)
    child_resource = add_resource(session, child, content_hash=HASH_C)
    add_snapshot(session, child_resource, text="Nur im Kindbereich", content_hash=HASH_C)
    add_open_task(session, context=child, title="Nur im Kindbereich")
    sibling = add_context(session, title="Lebensbereich Gesundheit", type="life_area")
    add_open_task(session, context=sibling, title="Joggen")

    packet = build(finance)

    serialised = json.dumps(packet.model_dump(mode="json"))
    assert "Nur im Kindbereich" not in serialised
    assert "Joggen" not in serialised
    assert "Kindbereich" not in serialised
    assert child.id not in {
        topic.context_id for topic in packet.topics if hasattr(topic, "context_id")
    }


def test_an_unknown_context_is_a_not_found_error(workspace_env):
    from organiser.services.domain import NotFoundError

    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()

    with pytest.raises(NotFoundError):
        workspace_context(session, "gibt-es-nicht", store)

    with pytest.raises(WorkspaceSelectionError):
        workspace_context(session, "  ", store)
