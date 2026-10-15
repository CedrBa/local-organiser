"""Determinism contracts for workspace."""

from __future__ import annotations

import json
from datetime import timedelta
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_C,
    add_context,
    add_open_task,
    add_resource,
    add_snapshot,
    add_topic,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env
from support.workspace_context import (
    build as build,
    labels as labels,
    hashlib_hex as hashlib_hex,
)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_repeated_packets_are_byte_identical(finance):
    first = build(finance).model_dump_json()
    second = build(finance).model_dump_json()
    third = build(finance).model_dump_json()

    assert first == second == third


def test_the_packet_carries_no_generation_timestamp(finance):
    serialised = build(finance).model_dump_json()
    today = "2026-09-30"

    # Only stored data may appear; a "now" would break the manifest comparison.
    assert '"generated_at"' not in serialised
    assert today not in json.loads(serialised)["freshness"].get("reason", "")


def test_task_order_is_due_first_then_creation_then_id(finance):
    session = finance["session"]
    context = finance["context"]
    without_due = add_open_task(
        session, context=context, title="Ohne Frist", created_at=BASE + timedelta(days=9)
    )
    soon = add_open_task(
        session,
        context=context,
        title="Morgen",
        due_at=BASE + timedelta(hours=2),
        created_at=BASE + timedelta(days=8),
    )
    with_open_task_status = add_open_task(
        session, context=context, title="In Arbeit", status="in_progress"
    )

    packet = build(finance)
    order = [task.id for task in packet.current_tasks]

    assert order.index(soon.id) < order.index(without_due.id)
    assert with_open_task_status.status == "in_progress"
    assert order == sorted(order, key=lambda identifier: order.index(identifier))


def test_completed_tasks_are_not_current_but_stay_counted(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Kurs")
    open_task = add_open_task(session, context=context, title="Offen")
    done = add_open_task(session, context=context, title="Erledigt", status="done")

    packet = workspace_context(session, context.id, store)

    assert [task.id for task in packet.current_tasks] == [open_task.id]
    assert done.id not in {task.id for task in packet.current_tasks}
    assert packet.context.task_count == 2
    assert packet.context.open_task_count == 1


def test_resource_ranking_prefers_topic_and_task_relations(finance):
    session = finance["session"]
    context = finance["context"]
    unrelated = add_resource(
        session,
        context,
        title="Unverbunden",
        relative_path="sonstiges.md",
        content_hash=HASH_C,
        last_seen_at=BASE,
    )
    add_snapshot(session, unrelated, text="Nie verknuepft", content_hash=HASH_C)

    packet = build(finance)
    order = [resource.id for resource in packet.resources]

    assert order.index(finance["worksheet"].id) < order.index(unrelated.id)
    assert order.index(finance["lecture"].id) < order.index(unrelated.id)


def test_explicit_focus_pulls_a_resource_to_the_front(finance):
    session, store = finance["session"], finance["store"]
    other = add_resource(
        session,
        finance["context"],
        title="Später",
        relative_path="spaeter.md",
        content_hash=HASH_C,
        last_seen_at=BASE + timedelta(days=5),
    )
    add_snapshot(
        session,
        other,
        text="Spaeterer Text",
        content_hash=HASH_C,
        created_at=BASE + timedelta(days=5),
    )

    packet = workspace_context(session, finance["context"].id, store, resource_ids=[other.id])

    assert packet.resources[0].id == other.id
    assert packet.selection.resource_ids == (other.id,)


def test_focus_on_a_topic_surfaces_its_resources_and_topic_file(finance):
    session, store = finance["session"], finance["store"]
    unlinked = add_resource(
        session,
        finance["context"],
        title="Nicht verknuepft",
        relative_path="nix.md",
        content_hash=HASH_C,
    )
    add_snapshot(session, unlinked, text="Nicht verknuepft", content_hash=HASH_C)

    packet = workspace_context(
        session, finance["context"].id, store, topic_ids=[finance["blocked"].id]
    )

    assert packet.topics[0].id == finance["blocked"].id
    assert "topic:" + finance["blocked"].knowledge_key in labels(packet)
    assert packet.selection.topic_ids == (finance["blocked"].id,)


def test_focus_is_not_lost_when_the_canonical_order_would_push_it_out(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Viele Topics")
    for index in range(25):
        add_topic(session, context, title=f"AAA {index:02d}", status="done")
    wanted = add_topic(session, context, title="ZZZ wanted", status="in_progress")

    packet = workspace_context(session, context.id, store, topic_ids=[wanted.id])

    assert packet.topics[0].id == wanted.id
    # 26 topics in the context, 20 shown: the omission is counted, not hidden.
    assert packet.selection.omitted_counts["topics"] == 6
