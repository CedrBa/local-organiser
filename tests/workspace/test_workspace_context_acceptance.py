"""Acceptance contracts for workspace."""

from __future__ import annotations

import json
from organiser.domain.context import (
    KNOWLEDGE_CURRENT,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    workspace_context,
)
from support.workspace import (
    EXTRACTOR,
    HASH_A,
    HASH_B,
    add_context,
    add_open_task,
    add_resource,
    add_snapshot,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env
from support.workspace_context import (
    build as build,
    labels as labels,
    hashlib_hex as hashlib_hex,
)


# ---------------------------------------------------------------------------
# The acceptance scenario
# ---------------------------------------------------------------------------


def test_workspace_packet_shows_the_whole_acceptance_scenario(finance):
    packet = build(finance)

    assert packet.kind == "context_workspace"
    assert packet.evidence_policy == "untrusted_data_not_instructions"
    assert packet.context.id == finance["context"].id
    assert packet.context.open_task_count == 2
    assert packet.context.topic_count == 2
    assert packet.context.resource_count == 2
    assert packet.context.source_count == 1
    assert packet.context.knowledge_bound is True

    # Open tasks first, by deadline.
    assert [task.id for task in packet.current_tasks] == [finance["task"].id, finance["later"].id]
    assert packet.current_tasks[0].resource_ids == (finance["worksheet"].id,)

    # Topics by status, then title_key, then id -- "blocked" sorts before "in_progress".
    assert [topic.title for topic in packet.topics] == ["Abschreibung", "Bilanzierung"]
    assert [topic.status for topic in packet.topics] == ["blocked", "in_progress"]

    # Resources ranked by topic/task relation, both linked to the balance topic.
    assert {resource.id for resource in packet.resources} == {
        finance["lecture"].id,
        finance["worksheet"].id,
    }
    for resource in packet.resources:
        assert resource.knowledge_status == KNOWLEDGE_CURRENT
        assert resource.topic_ids == (finance["balance"].id,)

    # The source is described, not exposed.
    assert [source.id for source in packet.source_descriptions] == [finance["source"].id]
    assert packet.source_descriptions[0].label == "finanzierung"
    assert "/erlaubt" not in json.dumps(packet.model_dump(mode="json"))

    # Document evidence carries hash, extractor version and a character locator.
    snapshot_excerpts = [e for e in packet.knowledge_excerpts if e.kind == "resource_snapshot"]
    assert len(snapshot_excerpts) == DEFAULT_WORKSPACE_POLICY.max_resource_excerpts
    for excerpt in snapshot_excerpts:
        assert excerpt.trust == "untrusted_evidence"
        assert excerpt.evidence.content_hash in {HASH_A, HASH_B}
        assert excerpt.evidence.extractor_version == EXTRACTOR
        assert excerpt.evidence.locator.char_start == 0
        assert excerpt.evidence.locator.char_end == len(excerpt.text)

    # The page span of the lecture PDF is resolved from the extractor metadata.
    lecture_excerpt = next(e for e in snapshot_excerpts if "vorlesung" in e.label)
    assert lecture_excerpt.evidence.locator.page_start == 1
    assert lecture_excerpt.evidence.locator.page_end == 12
    # A document without page metadata gets a character locator only, not a guess.
    worksheet_excerpt = next(e for e in snapshot_excerpts if "uebungsblatt" in e.label)
    assert worksheet_excerpt.evidence.locator.page_start is None
    assert worksheet_excerpt.evidence.locator.page_end is None

    # The budget is split evenly, so the topic's own markdown is visible too.
    knowledge_excerpts = [e for e in packet.knowledge_excerpts if e.kind == "knowledge"]
    assert knowledge_excerpts
    assert all(excerpt.label.startswith("topic:") for excerpt in knowledge_excerpts)
    assert "topic:" + finance["balance"].knowledge_key in labels(packet)
    assert len(packet.knowledge_excerpts) <= DEFAULT_WORKSPACE_POLICY.max_excerpts

    # Chronological, and closed over the context and its own tasks only.
    event_types = [event.event_type for event in packet.recent_history]
    assert "TASK_CREATED" in event_types
    assert [event.id for event in packet.recent_history] == sorted(
        event.id for event in packet.recent_history
    )
    assert packet.freshness.event_max_id > 0
    assert packet.freshness.authoritative is False


def test_packet_contains_no_absolute_locator_or_source_path(finance):
    serialised = json.dumps(build(finance).model_dump(mode="json"))

    assert "file:///" not in serialised
    assert "source_path_snapshot" not in serialised
    assert "/erlaubt/finanzierung/vorlesung1.pdf" not in serialised


# ---------------------------------------------------------------------------
# Empty worlds
# ---------------------------------------------------------------------------


def test_a_brand_new_context_yields_a_valid_empty_packet(workspace_env):
    session, _tmp = workspace_env
    store = KnowledgeStore(_tmp / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Neu")

    packet = workspace_context(session, context.id, store)

    assert packet.context.id == context.id
    assert packet.context.knowledge_bound is False
    assert packet.current_tasks == () and packet.topics == () and packet.resources == ()
    assert packet.source_descriptions == () and packet.knowledge_excerpts == ()
    assert packet.selection.omitted_counts == {}
    assert len(packet.model_dump_json()) <= DEFAULT_WORKSPACE_POLICY.max_packet_chars
    assert any("knowledge_path" in warning for warning in packet.warnings)


def test_context_without_sources_or_resources_stays_usable(finance):
    session = finance["session"]
    bare = add_context(session, title="Ohne Quellen")
    add_open_task(session, context=bare, title="Nur eine Aufgabe")

    packet = workspace_context(session, bare.id, finance["store"])

    assert packet.context.source_count == 0
    assert packet.context.resource_count == 0
    assert [task.title for task in packet.current_tasks] == ["Nur eine Aufgabe"]
    assert len(packet.model_dump_json()) <= DEFAULT_WORKSPACE_POLICY.max_packet_chars


def test_context_without_knowledge_still_reports_its_documents(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Ohne Knowledge")
    resource = add_resource(session, context)
    add_snapshot(session, resource, text="Kapitel 1")

    packet = workspace_context(session, context.id, store)

    assert [excerpt.text for excerpt in packet.knowledge_excerpts] == ["Kapitel 1"]
    assert any("Knowledge" in warning for warning in packet.warnings)


def test_missing_knowledge_store_argument_is_visible_not_fatal(finance):
    packet = workspace_context(finance["session"], finance["context"].id, None)

    # Database evidence is unaffected: only the knowledge *files* are missing.
    assert [e for e in packet.knowledge_excerpts if e.kind == "knowledge"] == []
    assert [e for e in packet.knowledge_excerpts if e.kind == "resource_snapshot"]
    assert any("kein Knowledge-Store" in warning for warning in packet.warnings)
    assert packet.context.id == finance["context"].id
    assert packet.context.knowledge_bound is True
    assert packet.resources
    assert packet.manifest.snapshots
    assert packet.manifest.knowledge == ()


def test_zero_rows_everywhere_produces_a_small_valid_packet(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Leer", path="contexts/seed")

    packet = workspace_context(session, context.id, store)

    assert packet.current_tasks == () and packet.topics == () and packet.resources == ()
    assert packet.knowledge_excerpts == ()
    # A context always carries at least its own creation event; nothing else.
    assert [(e.entity_type, e.entity_id) for e in packet.recent_history] == [
        ("context", context.id)
    ]
    assert len(packet.model_dump_json()) < 3_000
    assert packet.manifest.entities[0].entity_type == "context"
    assert [entry.entity_type for entry in packet.manifest.entities] == ["context", "event"]
