"""Serialization budgets contracts for workspace."""

from __future__ import annotations

import json
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    WorkspacePolicy,
    workspace_context,
)
from support.workspace import (
    add_context,
    add_event,
    add_open_task,
    add_resource,
    add_snapshot,
    add_source,
    add_topic,
    initialise_knowledge,
    link_resource_topic,
    write_knowledge,
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


def test_huge_event_payloads_cost_bounded_deserialisation_work(counted_session):
    """Twenty events with multi-megabyte payloads used to deserialise all of them.

    ``events.payload`` is the only column in the schema without a size constraint, and
    a legacy ``CONTEXT_SOURCE_ADDED`` row carries whatever its writer stored. The size
    is therefore decided in SQL, and this counts the characters that ever reach the
    JSON layer -- the work, not a stopwatch.
    """
    session, seen = counted_session
    context = add_context(session, title="Riesige Events")
    for index in range(DEFAULT_WORKSPACE_POLICY.max_events):
        add_event(
            session,
            entity_type="context",
            entity_id=context.id,
            event_type=f"RIESIG{index}",
            payload={"root_texts": ["x" * 40 for _ in range(60_000)], "i": index},
        )
    # One ordinary event *after* them, so it is inside the newest-``max_events``
    # window and the counter is demonstrably wired up rather than simply never called.
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="KLEIN",
        payload={"title": "Uebungsblatt"},
    )
    seen.clear()

    packet = workspace_context(session, context.id, None)

    policy = DEFAULT_WORKSPACE_POLICY
    assert sum(seen) <= policy.max_event_payload_bytes, "ein einzelnes Payload wurde nicht begrenzt"
    assert sum(seen) <= policy.max_events * policy.max_event_payload_bytes
    oversized = [item for item in packet.recent_history if item.event_type.startswith("RIESIG")]
    assert len(oversized) == policy.max_events - 1
    assert all(item.payload_truncated for item in oversized)
    assert all("nicht geladen" in json.dumps(item.payload) for item in oversized)
    assert packet.selection.omitted_counts["event_payloads"] == len(oversized)
    assert len(packet.model_dump_json()) <= policy.max_packet_chars


def test_the_reduction_loop_serialises_a_bounded_amount_of_json(workspace_env, monkeypatch):
    """Only the final size was bounded; the work of getting there was not.

    ``_assemble`` measures the packet after every pass and re-serialises it, so the
    request cost the *number of passes* times the packet size. Both are now bounded
    explicitly, and this asserts the bound structurally instead of by wall clock.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    long_title = "Bilanzierung " * 2_000
    context = add_context(session, title=long_title, description="D" * 40_000)
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path, title=long_title)
    for index in range(DEFAULT_WORKSPACE_POLICY.max_topics):
        topic = add_topic(session, context, title=f"{long_title} {index}")
        resource = add_resource(
            session,
            context,
            title=long_title,
            relative_path=f"{'p' * 300}{index}.md",
            content_hash=f"{index + 1:064x}"[-64:],
        )
        add_snapshot(session, resource, text="Y" * 4_000, content_hash=f"{index + 1:064x}"[-64:])
        link_resource_topic(session, resource, topic)
        write_knowledge(
            store, context.knowledge_path, f"topics/{topic.knowledge_key}.md", "Z" * 4_000
        )
        add_open_task(session, context=context, title=long_title, description="D" * 4_000)
        add_event(
            session,
            entity_type="context",
            entity_id=context.id,
            event_type="E" * 300,
            payload={"values": ["x" * 3_000 for _ in range(50)]},
        )

    from organiser.domain.context import ContextWorkspacePacket

    policy = DEFAULT_WORKSPACE_POLICY
    passes: list[int] = []
    original = ContextWorkspacePacket.model_dump_json

    def counting(self, *args, **kwargs):
        rendered = original(self, *args, **kwargs)
        passes.append(len(rendered))
        return rendered

    monkeypatch.setattr(ContextWorkspacePacket, "model_dump_json", counting)
    packet = workspace_context(session, context.id, store)

    assert passes, "der Test hat kein Pass vermessen"
    assert len(passes) <= policy.max_assemble_passes + 1
    # The pass that pushed the accumulated work over the limit was already running, so
    # the guarantee is "the limit, plus at most one more packet" -- not the bare limit.
    assert sum(passes) <= policy.max_serialized_chars_total + max(passes)
    assert len(packet.model_dump_json()) <= policy.max_packet_chars
    assert packet.selection.omitted_counts


def test_an_absolute_source_path_never_reaches_recent_history(workspace_env):
    """The packet promises no host addresses -- and history is read by the prompts.

    ``ContextSource.path`` is deliberately kept out of the packet and out of the
    manifest. A legacy ``CONTEXT_SOURCE_ADDED`` event, however, stores the absolute path
    it registered inside its *payload*, so the guarantee held everywhere except in
    ``recent_history``. The key and the shape stay; only the address is withheld.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Quelle im Event")
    add_source(session, context, path="/erlaubt/finanzierung/vorlesung.pdf")
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="CONTEXT_SOURCE_ADDED",
        payload={
            "path": "/erlaubt/finanzierung/vorlesung.pdf",
            "locator": "file:///erlaubt/finanzierung/vorlesung.pdf",
            "windows_path": "C:\\Kurs\\vorlesung.pdf",
            "source_type": "folder",
            "title": "Vorlesung",
        },
    )

    packet = workspace_context(session, context.id, store)

    rendered = packet.model_dump_json()
    assert "/erlaubt/finanzierung/vorlesung.pdf" not in rendered
    assert "file:///erlaubt" not in rendered
    assert "C:\\\\Kurs" not in rendered
    event = packet.recent_history[-1]
    assert event.payload is not None
    assert event.payload["path"] == "<absolute Pfad ausgelassen>"
    assert event.payload["locator"] == "<absolute Pfad ausgelassen>"
    assert event.payload["windows_path"] == "<absolute Pfad ausgelassen>"
    # The harmless fields survive, so the event still carries provenance.
    assert event.payload["source_type"] == "folder"
    assert event.payload["title"] == "Vorlesung"
    assert event.payload_truncated is True


# ---------------------------------------------------------------------------
# F4 -- "not readable" versus "readable but out of budget"
# ---------------------------------------------------------------------------


def test_readable_but_budget_omitted_markdown_is_not_called_unreadable(workspace_env):
    """A cumulative read counter could only say "reads happened", not "reads worked".

    ``overview.md`` was read successfully and then dropped because the excerpt budget
    was already spent. The packet reported "3 knowledge files present, none readable"
    -- a data-loss report for a file that was sitting right there -- and counted the
    same candidates a second time under ``knowledge_excerpts``.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Lesbar aber zu klein")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    # Readable, and big enough that the excerpt cannot survive the packet budget.
    (root / context.knowledge_path / "overview.md").write_text("L" * 3_000, encoding="utf-8")
    policy = WorkspacePolicy(max_packet_chars=2_600, max_excerpts=2, max_resource_excerpts=0)

    packet = workspace_context(session, context.id, store, policy)

    assert packet.knowledge_excerpts == ()
    assert not any("keiner im Packet lesbar" in warning for warning in packet.warnings)
    assert any(
        "lesbar" in warning and "Budgetgruenden" in warning for warning in packet.warnings
    ), packet.warnings
    (root / context.knowledge_path / "progress.md").unlink()
    packet = workspace_context(session, context.id, store, policy)

    omitted = packet.selection.omitted_counts
    # overview.md was read and held text but lost its slot; progress.md was opened and
    # really is not there; notes.md was never opened, so no claim is made about it.
    assert omitted["knowledge_excerpts"] == 2
    assert omitted["knowledge_unreadable"] == 1
    assert omitted.get("knowledge_empty") is None
    assert omitted["excerpts"] == 3
    # The reason keys are disjoint subsets of the total: no file is counted twice.
    assert (
        omitted["knowledge_excerpts"]
        + omitted["knowledge_unreadable"]
        + omitted.get("knowledge_empty", 0)
    ) <= omitted["excerpts"]


def test_genuinely_broken_markdown_is_still_reported_as_unreadable(workspace_env):
    """The distinction only means something if both halves are still visible."""
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Kaputt")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    dangling = root / context.knowledge_path / "notes.md"
    dangling.unlink()
    dangling.symlink_to(tmp_path / "gibt-es-nicht.md")

    packet = workspace_context(session, context.id, store, WorkspacePolicy(max_packet_chars=2_600))

    omitted = packet.selection.omitted_counts
    assert omitted.get("knowledge_unreadable", 0) >= 1
    assert any("nicht lesbar" in warning for warning in packet.warnings), packet.warnings


def test_the_omitted_counts_do_not_multiply_over_reduction_passes(workspace_env):
    """Every pass rebuilt the packet; counters summed per pass inflate the report."""
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Viele Durchlaeufe")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    initialise_knowledge(store, context.knowledge_path)
    for index in range(10):
        resource = add_resource(
            session,
            context,
            title=f"R{index}",
            relative_path=f"r{index}.md",
            content_hash=f"{index + 1:064x}"[-64:],
        )
        add_snapshot(session, resource, text="T" * 3_000, content_hash=f"{index + 1:064x}"[-64:])
    policy = WorkspacePolicy(max_packet_chars=4_000, max_excerpts=4, max_resource_excerpts=4)

    packet = workspace_context(session, context.id, store, policy)

    omitted = packet.selection.omitted_counts
    assert omitted["knowledge_excerpts"] <= DEFAULT_WORKSPACE_POLICY.max_legacy_topic_files_scanned
    # Three readable core files, two missing ones -- never more than what exists.
    assert omitted.get("knowledge_excerpts", 0) + omitted.get("knowledge_unreadable", 0) <= 5
