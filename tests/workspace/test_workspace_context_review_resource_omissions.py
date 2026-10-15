"""Resource omissions contracts for workspace."""

from __future__ import annotations

from datetime import timedelta
from organiser.domain.context import KNOWLEDGE_CURRENT
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    WorkspacePolicy,
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_A,
    HASH_B,
    add_context,
    add_resource,
    add_snapshot,
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
# F6 -- the contract must not advertise a state the builder never produces
# ---------------------------------------------------------------------------


def test_no_status_value_promises_a_budget_withheld_extract():
    """``snapshot_withheld_for_budget`` was declared, never produced, never handled.

    An unused member of a public ``Literal`` is worse than none: it tells a consumer a
    state exists, and it invites the false claim that a current resource with omitted
    text is in a degraded state. It is not -- ``knowledge_status`` answers "is the shown
    text the current version", which stays true, while ``evidence_truncated`` answers
    "is the text in the packet".
    """
    import organiser.domain.context as domain_context

    assert not hasattr(domain_context, "KNOWLEDGE_SNAPSHOT_WITHHELD")
    assert "snapshot_withheld_for_budget" not in str(domain_context.ResourceKnowledgeStatus)
    assert domain_context.ResourceKnowledgeStatus.__args__ == (
        "current",
        "no_current_hash",
        "resource_missing",
        "resource_archived",
        "snapshot_not_ready",
        "snapshot_failed",
        "snapshot_unsupported",
        "source_inactive",
    )


def test_a_current_resource_with_omitted_text_stays_current(workspace_env):
    """The two questions stay separate, and the omission stays visible."""
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Ausgelassener Text")
    resource = add_resource(session, context, content_hash=HASH_A)
    add_snapshot(session, resource, text="Aktueller Stand", content_hash=HASH_A)
    other = add_resource(
        session,
        context,
        relative_path="spaeter.md",
        content_hash=HASH_B,
        last_seen_at=BASE + timedelta(hours=1),
    )
    add_snapshot(session, other, text="Kuerzerer Text", content_hash=HASH_B)

    packet = workspace_context(session, context.id, store, WorkspacePolicy(max_excerpts=1))

    shown = {item.id: item for item in packet.resources}
    assert shown[resource.id].knowledge_status == KNOWLEDGE_CURRENT
    assert shown[resource.id].evidence_truncated is True
    # Still addressable: id, snapshot id and hash remain, which is what makes the
    # omission recoverable rather than a hole.
    assert shown[resource.id].snapshot_id is not None
    assert shown[resource.id].current_content_hash == HASH_A
    assert packet.selection.omitted_counts["resource_excerpt_texts"] >= 1
