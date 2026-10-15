"""Manifest audit contracts for workspace."""

from __future__ import annotations

from datetime import timedelta
from organiser.domain.workspace_contracts import (
    EntityManifestEntry,
    KnowledgeManifestEntry,
    WorkspaceManifest,
)
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    WorkspacePolicy,
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import (
    BASE,
    HASH_A,
    HASH_B,
    add_context,
    add_open_task,
    add_resource,
    add_snapshot,
    add_topic,
    initialise_knowledge,
    link_resource_topic,
    link_task_resource,
    write_knowledge,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_audit import (
    build as build,
)


def test_a_negative_policy_cannot_invert_a_bound(finance):
    """``candidates[:-1]`` would have emitted nine excerpts against a cap of four."""
    packet = build(finance, WorkspacePolicy(max_excerpts=-1, max_tasks=-5, max_sources=-2))

    assert len(packet.knowledge_excerpts) <= DEFAULT_WORKSPACE_POLICY.max_excerpts
    assert len(packet.current_tasks) <= DEFAULT_WORKSPACE_POLICY.max_tasks
    assert len(packet.source_descriptions) <= DEFAULT_WORKSPACE_POLICY.max_sources
    assert len(packet.model_dump_json()) <= DEFAULT_WORKSPACE_POLICY.max_packet_chars


# ---------------------------------------------------------------------------
# The apply gate must not be bricked, and must not leak
# ---------------------------------------------------------------------------


def test_a_long_titled_context_verifies_unchanged(workspace_env):
    """The manifest holds bounded text; comparing it against the raw column reported a
    change for untouched data and closed the apply gate permanently."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    long_title = "Bilanzierung " * 400
    context = add_context(session, title=long_title, type="course", description="D" * 5_000)
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    topic = add_topic(session, context, title="T" * 900)
    resource = add_resource(
        session, context, title=long_title, relative_path="r.md", content_hash=HASH_A
    )
    add_snapshot(session, resource, text="Text", content_hash=HASH_A)
    link_resource_topic(session, resource, topic)
    write_knowledge(store, context.knowledge_path, f"topics/{topic.knowledge_key}.md", "Inhalt\n")
    task = add_open_task(session, context=context, title=long_title, description="E" * 3_000)
    link_task_resource(session, task, resource)

    packet = workspace_context(session, context.id, store)
    result = verify_workspace_manifest(session, store, packet.manifest)

    assert result.unchanged is True, result.describe()
    assert len(packet.context.title) == DEFAULT_WORKSPACE_POLICY.max_title_chars


def test_the_excerpt_citing_snapshot_is_itself_recorded(workspace_env):
    """Recording only the newest snapshot left the cited one outside the precondition."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Zwei Versionen")
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
    cited = [
        excerpt.source_id
        for excerpt in packet.knowledge_excerpts
        if excerpt.kind == "resource_snapshot"
    ]
    recorded = {entry.snapshot_id for entry in packet.manifest.snapshots}

    assert cited
    assert set(cited) <= recorded
    # The newest snapshot is recorded as well, because its status is what was shown.
    assert packet.resources[0].snapshot_id in recorded
    assert len(packet.manifest.snapshots) == 2


def test_an_externally_crafted_manifest_cannot_smuggle_an_absolute_path(finance):
    """A hand-built manifest could name ``source_path_snapshot`` and have the verifier
    echo the host path back -- exactly what the packet promises never to expose."""
    manifest = WorkspaceManifest(
        version=1,
        entities=(
            EntityManifestEntry(
                entity_type="resource",
                entity_id=finance["lecture"].id,
                selected_fields={
                    "source_path_snapshot": "/home/operator/GEHEIM/skript.pdf",
                    "locator": "file:///home/operator/GEHEIM/skript.pdf",
                    "__dict__": "x",
                },
            ),
        ),
    )

    result = verify_workspace_manifest(finance["session"], finance["store"], manifest)

    assert result.unchanged is False
    refused = {change.field: change for change in result.changes if change.kind == "manifest"}
    assert set(refused) == {"source_path_snapshot", "locator", "__dict__"}
    assert all(change.actual == "unverifiable" for change in refused.values())
    # The manifest's own claim is echoed, but no *current* value was read for a field
    # outside the whitelist -- that is what would have leaked the host path.
    assert all("GEHEIM" not in change.actual for change in result.changes)
    assert all(
        change.actual == "unverifiable" for change in result.changes if change.kind == "manifest"
    )
    assert not any(
        isinstance(getattr(finance["lecture"], field, None), str) and "GEHEIM" in change.actual
        for change in result.changes
        for field in ("source_path_snapshot", "locator")
    )


def test_a_manifest_may_not_read_another_contexts_knowledge_file(finance):
    """Otherwise verification is a SHA-256 oracle over arbitrary markdown below the root.

    The address is checked against the manifest's *own* context entity **and** against
    the binding that context holds in the database right now. Either check alone would
    be enough to keep the promise: the recorded one because both values come from the
    same untrusted record, the current one because a rebound context must not have its
    old markdown hashed any more.
    """
    victim = "contexts/ein-anderer-context/overview.md"
    (finance["store"].root / victim).parent.mkdir(parents=True, exist_ok=True)
    (finance["store"].root / victim).write_text("fremdes Markdown", encoding="utf-8")
    foreign_entry = KnowledgeManifestEntry(
        context_id=finance["context"].id, relative_path=victim, content_hash="0" * 64
    )

    # No context entity at all: there is nothing to accept an address against.
    without_anchor = WorkspaceManifest(
        version=1, entities=(), snapshots=(), knowledge=(foreign_entry,)
    )
    result = verify_workspace_manifest(finance["session"], finance["store"], without_anchor)

    assert result.unchanged is False
    assert any(change.field == "context_id" for change in result.changes)
    assert "0" * 64 not in result.describe()

    # The real anchor plus a foreign address: the path check refuses it.
    anchored = build(finance).manifest.model_copy(update={"knowledge": (foreign_entry,)})
    result = verify_workspace_manifest(finance["session"], finance["store"], anchored)

    assert result.unchanged is False
    assert any(
        change.field == "knowledge_path" and change.actual == "foreign_path"
        for change in result.changes
    ), result.changes
    assert "0" * 64 not in result.describe(), "fremde Dateien werden nicht gehasht"


def test_verification_needs_the_policy_the_packet_was_built_with(finance):
    """The manifest holds bounded text, so the comparison bound must be the same one."""
    narrow = WorkspacePolicy(max_title_chars=20)
    packet = build(finance, narrow)

    assert (
        verify_workspace_manifest(
            finance["session"], finance["store"], packet.manifest, narrow
        ).unchanged
        is True
    )
    # Verifying with a wider bound compares 20 recorded characters against 200 stored.
    assert (
        verify_workspace_manifest(finance["session"], finance["store"], packet.manifest).unchanged
        is False
    )
