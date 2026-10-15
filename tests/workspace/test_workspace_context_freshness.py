"""Freshness contracts for workspace."""

from __future__ import annotations

import json
from datetime import timedelta
import pytest
from organiser.domain.context import (
    KNOWLEDGE_CURRENT,
    KNOWLEDGE_NO_CURRENT_HASH,
    KNOWLEDGE_RESOURCE_ARCHIVED,
    KNOWLEDGE_RESOURCE_MISSING,
    KNOWLEDGE_SNAPSHOT_FAILED,
    KNOWLEDGE_SNAPSHOT_UNSUPPORTED,
    KNOWLEDGE_SOURCE_INACTIVE,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_A,
    HASH_B,
    add_context,
    add_resource,
    add_snapshot,
    add_source,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env
from support.workspace_context import (
    build as build,
    labels as labels,
    hashlib_hex as hashlib_hex,
)


# ---------------------------------------------------------------------------
# Freshness: no stale text as current
# ---------------------------------------------------------------------------


def test_a_resource_without_a_current_hash_shows_no_text(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Ohne Hash")
    resource = add_resource(session, context, content_hash=None)
    add_snapshot(session, resource, text="Alter Extrakt", content_hash=HASH_A)

    packet = workspace_context(session, context.id, store)

    resource_view = packet.resources[0]
    assert resource_view.knowledge_status == KNOWLEDGE_NO_CURRENT_HASH
    assert resource_view.snapshot_id is not None, "der Snapshot bleibt sichtbar"
    assert "Alter Extrakt" not in json.dumps(packet.model_dump(mode="json"))


def test_a_hash_change_does_not_fall_back_to_the_old_extract(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Geaendert")
    resource = add_resource(session, context, content_hash=HASH_A)
    add_snapshot(session, resource, text="Alter Extrakt", content_hash=HASH_A)

    resource.current_content_hash = HASH_B
    session.commit()

    packet = workspace_context(session, context.id, store)

    assert packet.resources[0].knowledge_status == KNOWLEDGE_NO_CURRENT_HASH
    assert "Alter Extrakt" not in json.dumps(packet.model_dump(mode="json"))


@pytest.mark.parametrize(
    ("resource_status", "snapshot_status", "expected"),
    [
        ("missing", "ready", KNOWLEDGE_RESOURCE_MISSING),
        ("archived", "ready", KNOWLEDGE_RESOURCE_ARCHIVED),
        ("active", "failed", KNOWLEDGE_SNAPSHOT_FAILED),
        ("active", "unsupported", KNOWLEDGE_SNAPSHOT_UNSUPPORTED),
    ],
)
def test_non_ready_states_are_visible_and_never_carry_text(
    workspace_env, resource_status, snapshot_status, expected
):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Zustaende")
    resource = add_resource(session, context, status=resource_status)
    add_snapshot(
        session,
        resource,
        text="Text",
        content_hash=HASH_A,
        status=snapshot_status,
        created_at=BASE if snapshot_status != "ready" else BASE,
    )

    packet = workspace_context(session, context.id, store)

    assert packet.resources[0].knowledge_status == expected
    assert packet.knowledge_excerpts == ()
    assert packet.warnings


def test_an_inactive_source_invalidates_the_current_text(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Quelle inaktiv")
    source = add_source(session, context, status="archived")
    resource = add_resource(session, context, source=source)
    add_snapshot(session, resource, text="Text", content_hash=HASH_A)

    packet = workspace_context(session, context.id, store)

    assert packet.resources[0].knowledge_status == KNOWLEDGE_SOURCE_INACTIVE
    assert packet.knowledge_excerpts == ()
    assert any("Quelle ist nicht aktiv" in warning for warning in packet.warnings)


def test_a_newer_snapshot_for_an_older_hash_does_not_displace_the_current_one(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Neuere Extraktion")
    resource = add_resource(session, context, content_hash=HASH_A)
    add_snapshot(session, resource, text="Aktueller Text", content_hash=HASH_A, created_at=BASE)
    add_snapshot(
        session,
        resource,
        text="Text eines anderen Hashes",
        content_hash=HASH_B,
        created_at=BASE + timedelta(hours=2),
    )

    packet = workspace_context(session, context.id, store)

    assert packet.resources[0].knowledge_status == KNOWLEDGE_CURRENT
    assert [excerpt.text for excerpt in packet.knowledge_excerpts] == ["Aktueller Text"]
    assert packet.resources[0].snapshot_status == "ready"
    assert packet.resources[0].snapshot_id != packet.knowledge_excerpts[0].source_id
