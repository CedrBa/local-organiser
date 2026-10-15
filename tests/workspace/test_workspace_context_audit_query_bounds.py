"""Query bounds contracts for workspace."""

from __future__ import annotations

import json
import os
from datetime import timedelta
import pytest
from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
    MAX_TOPIC_FILE_SCAN,
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    workspace_context,
    workspace_excerpt_summary,
)
from support.workspace import (
    BASE,
    HASH_A,
    HASH_B,
    add_context,
    add_event,
    add_resource,
    add_snapshot,
    add_source,
    add_topic,
    write_knowledge,
    workspace_env as workspace_env,
)
from support.workspace import finance as finance, store as store
from support.workspace_context_audit import (
    build as build,
)


# ---------------------------------------------------------------------------
# The metadata scan must stay a metadata scan
# ---------------------------------------------------------------------------


def test_snapshot_text_is_never_loaded_for_a_resource_that_only_shows_metadata(
    workspace_env, monkeypatch
):
    """Twenty 200.000-character snapshots must not be fetched to render four excerpts."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Volle Schnappschausserei")
    source = add_source(session, context)
    for index in range(20):
        resource = add_resource(
            session,
            context,
            source=source,
            title=f"R{index:02d}",
            relative_path=f"r{index}.md",
            content_hash=f"{index:064x}"[-64:],
            last_seen_at=BASE + timedelta(minutes=index),
        )
        add_snapshot(
            session,
            resource,
            text="Y" * 199_995,
            content_hash=f"{index:064x}"[-64:],
            created_at=BASE + timedelta(minutes=index),
        )

    text_selects: list[str] = []
    original_execute = session.execute

    def tracking(statement, *args, **kwargs):
        rendered = str(statement)
        # A full projection of the blob; ``length(resource_snapshots.text)`` is the
        # cheap in-SQL emptiness probe and does not count.
        if "resource_snapshots.text" in rendered and "length(" not in rendered:
            text_selects.append(rendered)
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", tracking)
    packet = workspace_context(session, context.id, store)

    assert len(packet.knowledge_excerpts) <= DEFAULT_WORKSPACE_POLICY.max_excerpts
    # One full text read per rendered excerpt, and none for the metadata-only rows.
    assert len(text_selects) <= DEFAULT_WORKSPACE_POLICY.max_excerpts
    assert len(text_selects) >= 1


def test_a_noisy_resource_cannot_starve_another_resources_current_evidence(workspace_env):
    """The snapshot window is per resource, not one global window."""
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Sehr ungleiche Verteilung")
    noisy = add_resource(
        session, context, title="Noisy", relative_path="noisy.md", content_hash=HASH_A
    )
    for index in range(60):
        add_snapshot(
            session,
            noisy,
            text=f"Alt {index}",
            content_hash=f"{index:064x}"[-64:],
            status="failed",
            created_at=BASE + timedelta(hours=index),
        )
    add_snapshot(session, noisy, text="Aktueller Noisy-Text", content_hash=HASH_A, created_at=BASE)

    quiet = add_resource(
        session,
        context,
        title="Quiet",
        relative_path="quiet.md",
        content_hash=HASH_B,
        last_seen_at=BASE,
    )
    add_snapshot(session, quiet, text="Aktueller Quiet-Text", content_hash=HASH_B)

    packet = workspace_context(session, context.id, store)

    quiet_view = next(item for item in packet.resources if item.id == quiet.id)
    assert quiet_view.knowledge_status == "current"
    assert quiet_view.snapshot_id is not None
    texts = [excerpt.text for excerpt in packet.knowledge_excerpts]
    assert "Aktueller Quiet-Text" in texts or quiet_view.knowledge_status == "current"


def test_a_truncated_scan_is_reported_for_the_resource_that_caused_it(workspace_env):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Viele Extraktionen")
    resource = add_resource(session, context, content_hash=HASH_A)
    per_resource = DEFAULT_WORKSPACE_POLICY.max_snapshots_per_resource
    for index in range(per_resource + 5):
        add_snapshot(
            session,
            resource,
            text="x",
            content_hash=f"{index:064x}"[-64:],
            status="failed",
            created_at=BASE + timedelta(hours=index),
        )

    packet = workspace_context(session, context.id, store)

    assert any("neuesten" in warning and resource.id in warning for warning in packet.warnings), (
        packet.warnings
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [("failed", "fehlgeschlagen"), ("unsupported", "unterstuetzt")],
)
def test_failed_and_unsupported_snapshots_produce_a_warning_not_only_a_status(
    workspace_env, status, expected
):
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Zustand")
    resource = add_resource(session, context, content_hash=HASH_A)
    add_snapshot(session, resource, text="Text", content_hash=HASH_A, status=status)

    packet = workspace_context(session, context.id, store)

    assert packet.resources[0].knowledge_status.startswith("snapshot_")
    assert packet.knowledge_excerpts == ()
    assert any(resource.id in warning and expected in warning for warning in packet.warnings), (
        packet.warnings
    )


def test_one_request_reads_at_most_four_knowledge_files(finance, monkeypatch):
    """Reading every candidate first meant ~87 reads and ~20 MiB per GET."""
    session, store = finance["session"], finance["store"]
    for index in range(30):
        add_topic(session, context=finance["context"], title=f"Zusatz {index:02d}")
        write_knowledge(
            store,
            finance["knowledge_path"],
            f"topics/legacy-{index:02d}.md",
            f"Alt {index}\n" * 100,
        )
    reads: list[str] = []
    original = store.read_excerpt

    def counting(relative_path, max_chars, max_bytes=MAX_EXCERPT_BYTES):
        reads.append(relative_path)
        return original(relative_path, max_chars, max_bytes)

    monkeypatch.setattr(store, "read_excerpt", counting)
    packet = build(finance)

    assert len(reads) <= DEFAULT_WORKSPACE_POLICY.max_excerpts
    assert reads == [e.source_id for e in packet.knowledge_excerpts if e.kind == "knowledge"]


def test_the_task_packet_excerpt_reads_no_knowledge_file_at_all(finance, monkeypatch):
    """That DTO has no text field, so paying for markdown reads there is pure waste."""
    reads: list[str] = []
    original = finance["store"].read_excerpt

    def counting(relative_path, max_chars, max_bytes=MAX_EXCERPT_BYTES):
        reads.append(relative_path)
        return original(relative_path, max_chars, max_bytes)

    monkeypatch.setattr(finance["store"], "read_excerpt", counting)
    summary = workspace_excerpt_summary(finance["session"], finance["context"].id, finance["store"])

    assert reads == []
    assert summary.context_id == finance["context"].id
    assert summary.open_task_count == 2


def test_list_topic_files_bounds_the_names_it_reads(store, monkeypatch):
    """``sorted()`` over ``scandir`` consumed the whole directory before the slice."""
    directory = store.root / "contexts" / "c1" / "topics"
    directory.mkdir(parents=True)
    for index in range(MAX_TOPIC_FILE_SCAN + 500):
        (directory / f"t{index:05d}.md").write_text("x", encoding="utf-8")

    seen: list[str] = []
    original_scandir = os.scandir

    class counting:
        """A scandir result that counts names as it yields them."""

        def __init__(self, path):
            self._path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            with original_scandir(self._path) as handle:
                for entry in handle:
                    seen.append(entry.name)
                    yield entry

    monkeypatch.setattr(os, "scandir", counting)
    stems = store.list_topic_files("contexts/c1", limit=5)

    assert len(stems) == 5
    assert len(seen) <= MAX_TOPIC_FILE_SCAN


def test_a_deeply_nested_event_payload_is_not_fully_serialised(workspace_env):
    """Serialising the tail of a payload to render 200 characters is unbounded work.

    Two different bounds, on purpose. The SQL bound keeps a payload larger than the
    whole projection could ever show out of Python altogether; the depth bound keeps a
    payload that *is* loaded from being walked past ``max_event_payload_depth``.
    """
    session, tmp_path = workspace_env
    store = KnowledgeStore(tmp_path / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Tief")
    huge = {"a": {"b": {"c": {"d": {"e": ["x" * 1_000_000 for _ in range(20)]}}}}}
    add_event(session, entity_type="context", entity_id=context.id, event_type="TIEF", payload=huge)
    # Same nesting, small enough to be materialised: the depth cut has to do its job.
    deep = {"a": {"b": {"c": {"d": {"e": ["x" * 400 for _ in range(20)]}}}}}
    add_event(
        session, entity_type="context", entity_id=context.id, event_type="TIEF_KLEIN", payload=deep
    )

    packet = workspace_context(session, context.id, store)

    event = next(item for item in packet.recent_history if item.event_type == "TIEF")
    assert event.payload_truncated is True
    assert "nicht geladen" in json.dumps(event.payload)
    small = next(item for item in packet.recent_history if item.event_type == "TIEF_KLEIN")
    rendered = json.dumps(small.payload)
    assert small.payload_truncated is True
    assert "cut at depth" in rendered
    assert len(rendered) < 1_000
