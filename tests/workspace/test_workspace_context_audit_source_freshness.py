"""Source freshness contracts for workspace."""

from __future__ import annotations

import json
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    workspace_context,
)
from support.workspace import (
    HASH_A,
    add_context,
    add_resource,
    add_snapshot,
    add_source,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_audit import (
    build as build,
)


# ---------------------------------------------------------------------------
# A stale source must not keep its document presented as current
# ---------------------------------------------------------------------------


def test_a_source_beyond_the_display_budget_still_controls_freshness(workspace_env):
    """The source-active check must not depend on how many sources are displayed.

    The display list holds ten sources. Reading the check from it meant a resource
    whose source was the eleventh answered "is my source active?" with "is my source in
    the first ten?", and an archived source kept its document shown as current.
    """
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Viele Quellen")
    old_sources = [
        add_source(session, context, path=f"/erlaubt/alt-{index:02d}") for index in range(12)
    ]
    target_source = add_source(session, context, path="/erlaubt/die-archivierte", status="archived")
    resource = add_resource(session, context, source=target_source, content_hash=HASH_A)
    add_snapshot(session, resource, text="TEXT AUS EINER ARCHIVIERTEN QUELLE", content_hash=HASH_A)

    packet = workspace_context(session, context.id, store)

    view = next(item for item in packet.resources if item.id == resource.id)
    assert view.knowledge_status == "source_inactive"
    assert "TEXT AUS EINER ARCHIVIERTEN QUELLE" not in json.dumps(packet.model_dump(mode="json"))
    assert any("Quelle ist nicht aktiv" in warning for warning in packet.warnings)
    # The check genuinely consulted all twelve sources, not the displayed ten.
    assert packet.context.source_count == 13
    assert len(packet.source_descriptions) == DEFAULT_WORKSPACE_POLICY.max_sources
    assert len(old_sources) == 12
    # The source that decided the conclusion is part of the apply precondition.
    manifest_sources = {
        e.entity_id for e in packet.manifest.entities if e.entity_type == "context_source"
    }
    assert target_source.id in manifest_sources


def test_a_source_of_another_context_never_grants_freshness(workspace_env):
    """``resources.source_id`` has no cross-context constraint; D must not trust it."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Eigener Kurs")
    other = add_context(session, title="Fremder Kurs")
    foreign_source = add_source(session, other, path="/erlaubt/fremd")
    resource = add_resource(session, context, source=foreign_source, content_hash=HASH_A)
    add_snapshot(session, resource, text="FREMDE QUELLE", content_hash=HASH_A)

    packet = workspace_context(session, context.id, store)

    view = next(item for item in packet.resources if item.id == resource.id)
    assert view.knowledge_status == "source_inactive"
    assert "FREMDE QUELLE" not in json.dumps(packet.model_dump(mode="json"))
