"""Read only contracts for workspace."""

from __future__ import annotations

import json
from organiser.db.models import Event, ResourceSnapshot
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import (
    verify_workspace_manifest,
    workspace_context,
    workspace_excerpt_summary,
)
from support.workspace import (
    EXTRACTOR,
    HASH_A,
    HASH_B,
    add_context,
    database_fingerprint,
    tree_fingerprint,
    write_knowledge,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env
from support.workspace_context import (
    build as build,
    labels as labels,
    hashlib_hex as hashlib_hex,
)


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


def test_building_the_packet_changes_neither_the_database_nor_the_knowledge_tree(finance):
    store = finance["store"]
    database = finance["tmp_path"] / "workspace.sqlite3"
    write_knowledge(
        store,
        finance["knowledge_path"],
        "notes.md",
        "# Notizen\n\n## 2026-09-30 08:00 UTC\n\nStand.\n",
    )

    before_tree = tree_fingerprint(store.root)
    before_db = database_fingerprint(database)

    for _ in range(3):
        build(finance)

    assert tree_fingerprint(store.root) == before_tree
    assert database_fingerprint(database) == before_db


def test_building_the_packet_does_not_create_a_knowledge_root(workspace_env):
    session, tmp_path = workspace_env
    missing_root = tmp_path / "gibt-es-nicht"
    store = KnowledgeStore(missing_root)
    context = add_context(session, title="Ohne Root")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()

    packet = workspace_context(session, context.id, store)

    assert packet.context.id == context.id
    assert not missing_root.exists()


def test_building_the_packet_opens_no_source_file(finance, tmp_path, monkeypatch):
    """The builder must never touch a source document, only the snapshot cache."""
    source_document = tmp_path / "quelle" / "vorlesung1.pdf"
    source_document.parent.mkdir()
    source_document.write_bytes(b"%PDF-1.7 quelle")
    opened: list[str] = []
    real_open = open

    def tracking_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", tracking_open)
    packet = build(finance)

    assert str(source_document) not in opened
    assert not any(entry.endswith(".pdf") for entry in opened)
    assert source_document.read_bytes() == b"%PDF-1.7 quelle"
    assert packet.resources


# ---------------------------------------------------------------------------
# Manifest and verification
# ---------------------------------------------------------------------------


def test_the_manifest_covers_every_used_entity(finance):
    packet = build(finance)
    manifest = packet.manifest
    by_type = {}
    for entry in manifest.entities:
        by_type.setdefault(entry.entity_type, []).append(entry.entity_id)

    assert by_type["context"] == [finance["context"].id]
    assert set(by_type["task"]) == {finance["task"].id, finance["later"].id}
    assert set(by_type["topic"]) == {finance["balance"].id, finance["blocked"].id}
    assert set(by_type["resource"]) == {finance["lecture"].id, finance["worksheet"].id}
    assert by_type["context_source"] == [finance["source"].id]
    assert len(by_type["event"]) == len(packet.recent_history)

    context_entry = manifest.entities[0]
    assert context_entry.updated_at is not None
    assert context_entry.selected_fields["knowledge_path"] == finance["knowledge_path"]
    # No absolute source path anywhere in the manifest.
    assert "/erlaubt" not in json.dumps(manifest.model_dump(mode="json"), default=str)

    # Snapshots carry original hash and extractor version, separately.
    for snapshot in manifest.snapshots:
        assert snapshot.content_hash in {HASH_A, HASH_B}
        assert snapshot.extractor_version == EXTRACTOR

    # Knowledge entries carry the real file hash of what was read.
    read_paths = {entry.relative_path for entry in manifest.knowledge}
    assert read_paths
    for entry in manifest.knowledge:
        assert entry.context_id == finance["context"].id
        assert len(entry.content_hash) == 64
    assert all(
        entry.relative_path == excerpt.source_id
        for entry, excerpt in zip(
            manifest.knowledge, [e for e in packet.knowledge_excerpts if e.kind == "knowledge"]
        )
    )


def test_verification_passes_for_an_untouched_workspace(finance):
    packet = build(finance)

    result = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)

    assert result.unchanged is True
    assert result.changes == ()
    assert result.checked_entities == len(packet.manifest.entities)
    assert result.describe() == "Manifest unveraendert"


def test_verification_detects_a_hand_edited_knowledge_file(finance):
    """A markdown edit produces no event, so only the file hash can catch it."""
    packet = build(finance)
    read_path = packet.manifest.knowledge[0].relative_path
    (finance["store"].root / read_path).write_text("# Von Hand geaendert\n", encoding="utf-8")

    result = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)

    assert result.unchanged is False
    assert any(
        change.kind == "knowledge"
        and change.field == "content_hash"
        and change.reference == read_path
        for change in result.changes
    )
    assert result.checked_knowledge_files == len(packet.manifest.knowledge)


def test_verification_detects_a_deleted_knowledge_file(finance):
    packet = build(finance)
    read_path = packet.manifest.knowledge[0].relative_path
    (finance["store"].root / read_path).unlink()

    result = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)

    assert result.unchanged is False
    assert any(
        change.kind == "knowledge" and change.field == "readable" and change.reference == read_path
        for change in result.changes
    )


def test_verification_detects_changed_db_state(finance):
    packet = build(finance)
    finance["balance"].status = "done"
    finance["context"].title = "Umbenannt"
    finance["session"].commit()

    result = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)

    assert result.unchanged is False
    fields = {change.field for change in result.changes}
    assert "status" in fields
    assert "title" in fields


def test_verification_detects_a_replaced_snapshot(finance):
    packet = build(finance)
    snapshot_id = packet.manifest.snapshots[0].snapshot_id
    session = finance["session"]
    snapshot = session.get(ResourceSnapshot, snapshot_id)
    snapshot.extractor_version = "text-v2"
    session.commit()

    result = verify_workspace_manifest(session, finance["store"], packet.manifest)

    assert result.unchanged is False
    assert any(change.field == "extractor_version" for change in result.changes)


def test_verification_detects_a_changed_event_payload(finance):
    packet = build(finance)
    session = finance["session"]
    event = session.query(Event).filter(Event.entity_type == "task").one()
    event.payload = {"title": "Nicht mehr das Original"}
    session.commit()

    result = verify_workspace_manifest(session, finance["store"], packet.manifest)

    assert result.unchanged is False
    assert any(change.field == "payload_hash" for change in result.changes)


def test_verification_detects_a_removed_row(finance):
    packet = build(finance)
    session = finance["session"]
    session.delete(session.get(Event, packet.manifest.entities[-1].entity_id))
    session.commit()

    result = verify_workspace_manifest(session, finance["store"], packet.manifest)

    assert result.unchanged is False
    assert any(change.kind == "missing" for change in result.changes)


def test_verification_is_read_only_and_repeatable(finance):
    packet = build(finance)
    before_tree = tree_fingerprint(finance["store"].root)
    before_db = database_fingerprint(finance["tmp_path"] / "workspace.sqlite3")

    first = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)
    second = verify_workspace_manifest(finance["session"], finance["store"], packet.manifest)

    assert first == second
    assert tree_fingerprint(finance["store"].root) == before_tree
    assert database_fingerprint(finance["tmp_path"] / "workspace.sqlite3") == before_db


def test_verification_without_a_store_reports_knowledge_as_unreadable(finance):
    packet = build(finance)

    result = verify_workspace_manifest(finance["session"], None, packet.manifest)

    assert result.unchanged is False
    assert all(change.kind == "knowledge" for change in result.changes)


# ---------------------------------------------------------------------------
# The small task-packet excerpt
# ---------------------------------------------------------------------------


def test_the_task_excerpt_is_metadata_only(finance):
    summary = workspace_excerpt_summary(finance["session"], finance["context"].id, finance["store"])
    serialised = summary.model_dump_json()

    assert summary.context_id == finance["context"].id
    assert summary.open_task_count == 2
    assert summary.topics and all(topic.id for topic in summary.topics)
    assert "Bilanzierung" in serialised
    # No untrusted document or markdown text may reach the interpreter/planner prompt.
    assert "Die Bewertung von Vermoegensgegenstaenden" not in serialised
    assert "Kursnotiz Rechnungswesen und Rechnungslegungsbilanz" not in serialised
    assert "Aktiva und Passiva" not in serialised
    assert "Erstes Kapitel gelesen" not in serialised
    assert len(serialised) < 3_000
    for item in summary.evidence:
        assert item.content_hash and item.source_id


def test_the_task_excerpt_reports_an_empty_context(finance):
    session, store = finance["session"], finance["store"]
    empty = add_context(session, title="Leerer Kurs")

    summary = workspace_excerpt_summary(session, empty.id, store)

    assert summary.context_id == empty.id
    assert summary.topics == () and summary.evidence == ()
    assert summary.knowledge_bound is False
