"""Link limits contracts for workspace."""

from __future__ import annotations

from datetime import timedelta
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    WorkspacePolicy,
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_A,
    add_context,
    add_open_task,
    add_resource,
    link_task_resource,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# F5 -- the link cap belongs to the recorded policy
# ---------------------------------------------------------------------------


def test_a_non_default_link_cap_verifies_unchanged(workspace_env):
    """The verifier used the module default, so a narrower manifest could never pass.

    ``max_ids_per_item`` was recorded in the manifest but not honoured while verifying:
    a packet built with three IDs per item was compared against five, and a proposal
    with a legitimate custom policy reported "changed" for untouched data -- a
    permanently closed apply gate.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Eigene Kappe")
    task = add_open_task(session, context=context)
    policy = WorkspacePolicy(max_ids_per_item=2)
    for index in range(7):
        resource = add_resource(
            session,
            context,
            relative_path=f"r{index}.md",
            content_hash=HASH_A,
            last_seen_at=BASE + timedelta(minutes=index),
        )
        link_task_resource(session, task, resource)

    packet = workspace_context(session, context.id, store, policy)

    assert len(packet.current_tasks[0].resource_ids) == 2
    result = verify_workspace_manifest(session, store, packet.manifest, policy)
    assert result.unchanged is True, result.describe()


def test_the_recorded_link_cap_still_detects_a_change_inside_it(workspace_env):
    """Passing the policy through must not turn the comparison into a no-op."""
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Eigene Kappe")
    task = add_open_task(session, context=context)
    policy = WorkspacePolicy(max_ids_per_item=2)
    resources = [
        add_resource(
            session,
            context,
            relative_path=f"r{index}.md",
            content_hash=HASH_A,
            last_seen_at=BASE + timedelta(minutes=index),
        )
        for index in range(7)
    ]
    # Fixed identifiers instead of a hunt for a "smaller" random UUID. With a cap of two
    # the recorded window is the two smallest ids of the link table, and searching random
    # ones for a third that sorts in front of them failed in roughly one run out of
    # twenty-seven: a regression test that fails on its own protects nothing.
    resources[1].id = "b-resource"
    resources[2].id = "c-resource"
    session.commit()
    session.expire_all()
    for resource in resources[1:3]:
        link_task_resource(session, task, resource)
    packet = workspace_context(session, context.id, store, policy)

    assert set(packet.current_tasks[0].resource_ids) == {"b-resource", "c-resource"}
    assert verify_workspace_manifest(session, store, packet.manifest, policy).unchanged is True

    # A third link that sorts *into* the recorded window: the smallest id wins the first
    # slot, so the compared pair changes without the entities themselves changing.
    intruder = add_resource(
        session,
        context,
        relative_path="extra.md",
        content_hash=HASH_A,
        last_seen_at=BASE + timedelta(minutes=7),
    )
    intruder.id = "a-resource"
    session.commit()
    link_task_resource(session, task, intruder)

    result = verify_workspace_manifest(session, store, packet.manifest, policy)
    assert result.unchanged is False
    assert any(change.field == "resource_ids" for change in result.changes), result.changes
