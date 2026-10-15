"""Read boundaries contracts for workspace."""

from __future__ import annotations

import os
from datetime import timedelta
import pytest
from organiser.domain.workspace_contracts import (
    EntityManifestEntry,
    KnowledgeManifestEntry,
    WorkspaceManifest,
)
from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
    KnowledgeStore,
    KnowledgeError,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    WorkspacePolicy,
    verify_workspace_manifest,
    workspace_context,
    workspace_excerpt_summary,
)
from support.workspace import (
    BASE,
    HASH_A,
    add_context,
    add_event,
    add_resource,
    add_snapshot,
    add_source,
    add_topic,
    initialise_knowledge,
    write_knowledge,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_audit import (
    build as build,
)


def test_a_failed_read_leaks_no_descriptors_either(store):
    directory = store.root / "contexts" / "c1" / "topics"
    directory.mkdir(parents=True)

    def open_descriptors() -> int:
        return len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else -1

    before = open_descriptors()
    for _ in range(100):
        with pytest.raises(KnowledgeError):
            store.read_excerpt("contexts/c1/topics/fehlt.md", 100)
    after = open_descriptors()

    if before >= 0:
        assert after - before <= 2, f"{after - before} Deskriptoren nach 100 Fehlschlaegen"


def test_an_unreadable_knowledge_file_releases_its_excerpt_slot(workspace_env):
    """A broken markdown file must not permanently cost a document excerpt."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Kaputtes Markdown")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    # Three core files, the first of them far above the hard read limit.
    (store.root / context.knowledge_path / "overview.md").write_text(
        "W" * (MAX_EXCERPT_BYTES + 1), encoding="utf-8"
    )
    for index in range(3):
        resource = add_resource(
            session,
            context,
            title=f"R{index}",
            relative_path=f"r{index}.md",
            content_hash=f"{index + 1:064x}"[-64:],
            last_seen_at=BASE + timedelta(minutes=index),
        )
        add_snapshot(
            session, resource, text=f"Text {index}", content_hash=f"{index + 1:064x}"[-64:]
        )

    packet = workspace_context(session, context.id, store)
    policy = DEFAULT_WORKSPACE_POLICY

    assert len(packet.knowledge_excerpts) == policy.max_excerpts
    assert any("KnowledgeExcerptTooLargeError" in warning for warning in packet.warnings)
    # progress.md and notes.md took the two slots the broken file released.
    knowledge = [
        excerpt.label for excerpt in packet.knowledge_excerpts if excerpt.kind == "knowledge"
    ]
    assert "context_core:progress.md" in knowledge
    assert "context_core:notes.md" in knowledge


def test_the_task_packet_excerpt_claims_no_false_knowledge_warning(finance):
    """It deliberately reads no markdown, so it must not claim none was readable.

    ``excerpts: N ausgelassen (Budget)`` stays correct -- that summary really does ask
    for zero excerpt slots. The claim that must not appear is "nothing readable",
    because nothing was even attempted.
    """
    summary = workspace_excerpt_summary(finance["session"], finance["context"].id, finance["store"])

    assert not any("keiner im Packet lesbar" in warning for warning in summary.warnings), (
        summary.warnings
    )
    assert not any("KnowledgeNotFoundError" in warning for warning in summary.warnings)


def test_a_source_of_another_context_is_still_in_the_precondition(workspace_env):
    """Moving it into this context flips the packet to current -- the old manifest has
    to notice."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Eigener Kurs")
    other = add_context(session, title="Fremder Kurs")
    foreign = add_source(session, other, path="/erlaubt/fremd")
    resource = add_resource(session, context, source=foreign, content_hash=HASH_A)
    add_snapshot(session, resource, text="FREMDE QUELLE", content_hash=HASH_A)

    packet = workspace_context(session, context.id, store)
    assert packet.resources[0].knowledge_status == "source_inactive"
    recorded = {
        entry.entity_id
        for entry in packet.manifest.entities
        if entry.entity_type == "context_source"
    }
    assert foreign.id in recorded, "die entscheidende Quelle fehlt im Manifest"

    foreign.context_id = context.id
    session.commit()
    after = workspace_context(session, context.id, store)
    assert after.resources[0].knowledge_status == "current"

    result = verify_workspace_manifest(session, store, packet.manifest)
    assert result.unchanged is False


def test_an_excerpt_outliving_its_resource_row_is_still_in_the_precondition(workspace_env):
    """The budget can empty the resource list while an excerpt survives."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Enger Rahmen")
    for index in range(12):
        add_topic(session, context, title="T" * 400 + f" {index:02d}")
    for index in range(12):
        resource = add_resource(
            session,
            context,
            title="R",
            relative_path=f"r{index}.md",
            content_hash=f"{index + 1:064x}"[-64:],
        )
        add_snapshot(session, resource, text="KURZ", content_hash=f"{index + 1:064x}"[-64:])

    packet = workspace_context(session, context.id, store, WorkspacePolicy(max_packet_chars=4_000))
    cited = [
        excerpt.source_id
        for excerpt in packet.knowledge_excerpts
        if excerpt.kind == "resource_snapshot"
    ]
    recorded = {entry.snapshot_id for entry in packet.manifest.snapshots}

    assert cited, "der Test hat keinen Snapshot-Auszug erzeugt"
    assert set(cited) <= recorded


def test_a_crafted_manifest_cannot_hash_another_context_by_lying_about_the_id(finance):
    """Both the path and the context id come from the same untrusted record."""

    victim = "contexts/fremder-context/overview.md"
    (finance["store"].root / victim).parent.mkdir(parents=True, exist_ok=True)
    (finance["store"].root / victim).write_text("fremdes Markdown", encoding="utf-8")
    import hashlib

    real = hashlib.sha256(b"fremdes Markdown").hexdigest()
    crafted = WorkspaceManifest(
        version=1,
        entities=(
            EntityManifestEntry(
                entity_type="context",
                entity_id=finance["context"].id,
                status="active",
                selected_fields={
                    "title": "Finanzierung und Rechnungswesen",
                    "type": "course",
                    "status": "active",
                    "description": None,
                    "parent_context_id": None,
                    "knowledge_path": finance["knowledge_path"],
                },
            ),
        ),
        knowledge=(
            KnowledgeManifestEntry(
                context_id="fremder-context", relative_path=victim, content_hash=real
            ),
        ),
    )

    result = verify_workspace_manifest(finance["session"], finance["store"], crafted)

    assert result.unchanged is False
    assert any(change.field == "context_id" for change in result.changes)
    assert real not in result.describe(), (
        "der Verifier darf keinen Hash fremder Dateien zurueckgeben"
    )


def test_a_long_event_entity_id_still_verifies_unchanged(workspace_env):
    """``entity_id`` is truncated in the packet, so the verifier must truncate it too."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Lange Kennung")
    long_id = "e" * 400
    add_event(session, entity_type="context", entity_id=context.id, event_type="X" * 300)
    event = (
        session.query(__import__("organiser.db.models", fromlist=["Event"]).Event)
        .order_by(__import__("organiser.db.models", fromlist=["Event"]).Event.id.desc())
        .first()
    )
    event.entity_id = long_id
    session.commit()

    packet = workspace_context(session, context.id, store)
    result = verify_workspace_manifest(session, store, packet.manifest)

    assert result.unchanged is True, result.describe()


def test_a_zero_warning_budget_cannot_slice_inverted(workspace_env):
    """``warnings[:-1]`` would have kept every warning but the last."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    for index in range(6):
        context = add_context(session, title=f"Kurs {index}")
        resource = add_resource(session, context, content_hash=HASH_A)
        add_snapshot(session, resource, text="x", content_hash=HASH_A, status="failed")

    packet = workspace_context(
        session,
        add_context(session, title="Ohne").id,
        store,
        WorkspacePolicy(max_warnings=0),
    )

    assert packet.warnings == ()


def test_the_summary_slices_follow_the_policy(finance):
    narrow = WorkspacePolicy(summary_max_topics=1, summary_max_evidence=1, summary_max_warnings=1)

    summary = workspace_excerpt_summary(
        finance["session"], finance["context"].id, finance["store"], narrow
    )

    assert len(summary.topics) == 1
    assert len(summary.evidence) <= 1
    assert len(summary.warnings) <= 1


def test_the_emergency_description_reduction_is_announced(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Lang", description="D" * 4_000)
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    for index in range(6):
        resource = add_resource(
            session,
            context,
            title="T" * 400,
            relative_path=f"r{index}.md",
            content_hash=f"{index + 1:064x}"[-64:],
        )
        add_snapshot(session, resource, text="Y" * 4_000, content_hash=f"{index + 1:064x}"[-64:])
        write_knowledge(store, context.knowledge_path, f"notes{index}.md", "x")
    write_knowledge(store, context.knowledge_path, "notes.md", "Z" * 4_000)

    packet = workspace_context(session, context.id, store, WorkspacePolicy(max_packet_chars=3_000))

    if packet.context.description is None:
        assert "context.description" in packet.selection.omitted_counts
        assert any("context.description" in warning for warning in packet.warnings)
