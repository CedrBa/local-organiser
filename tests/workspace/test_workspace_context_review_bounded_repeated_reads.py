"""Bounded repeated reads contracts for workspace."""

from __future__ import annotations

import os
from datetime import timedelta
from organiser.domain.workspace_contracts import KnowledgeManifestEntry, WorkspaceManifest
from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
    MAX_TOPIC_FILE_SCAN,
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    WorkspacePolicy,
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import (
    BASE,
    add_context,
    add_resource,
    add_snapshot,
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


def test_no_knowledge_file_is_read_twice_no_matter_how_many_passes_run(workspace_env, monkeypatch):
    """``_assemble`` may rebuild the packet dozens of times; a read happens once.

    The read is cached on the candidate, not on the pass. That is the difference between
    "four excerpts plus a bounded reserve" and "four excerpts times the number of
    reduction passes" -- the packet is reassembled after every single halving, so a
    per-pass read would be a per-pass cost of up to 256 KiB of disk traffic and SHA-256
    work for a file whose text is already in hand.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Viele Durchlaeufe")
    context.knowledge_path = f"contexts/{context.id}"
    session.commit()
    for index in range(20):
        write_knowledge(store, context.knowledge_path, f"notes{index:02d}.md", f"Notiz {index}.\n")
    for index in range(20):
        resource = add_resource(
            session,
            context,
            relative_path=f"r{index:02d}.md",
            content_hash=f"{index + 1:064x}"[-64:],
            last_seen_at=BASE + timedelta(minutes=index),
        )
        add_snapshot(session, resource, text="T" * 4_000, content_hash=f"{index + 1:064x}"[-64:])

    calls: list[str] = []
    original = store.read_excerpt

    def counting(relative_path, max_chars, max_bytes=MAX_EXCERPT_BYTES):
        calls.append(relative_path)
        return original(relative_path, max_chars, max_bytes)

    monkeypatch.setattr(store, "read_excerpt", counting)
    policy = WorkspacePolicy(max_packet_chars=6_000)
    packet = workspace_context(session, context.id, store, policy)

    from organiser.domain.context import ContextWorkspacePacket

    passes: list[int] = []
    rendered = ContextWorkspacePacket.model_dump_json

    def counting_dump(self, *args, **kwargs):
        text = rendered(self, *args, **kwargs)
        passes.append(len(text))
        return text

    monkeypatch.setattr(ContextWorkspacePacket, "model_dump_json", counting_dump)
    calls.clear()
    packet = workspace_context(session, context.id, store, policy)

    assert len(passes) > 3, "der Test hat keine Reduktionsschleife provoziert"
    assert len(calls) <= policy.max_excerpts + policy.max_excerpt_read_attempts, calls
    assert len(set(calls)) == len(calls), "eine Knowledge-Datei wurde mehrfach gelesen"
    assert len(packet.model_dump_json()) <= policy.max_packet_chars


# ---------------------------------------------------------------------------
# The verifier must not pass an attacker's own value through as an API error
# ---------------------------------------------------------------------------


def test_an_unfiltered_attacker_value_never_reaches_the_client(finance):
    """What the verifier reports is what a caller may show a user.

    ``changes[].actual`` is derived from a manifest the caller supplied. A raw echo of
    an arbitrary string would end up in an error body, so the verifier has to name the
    *reason* it refused rather than repeat what it refused.
    """
    session, store = finance["session"], finance["store"]
    hostile = "</script><script>alert(1)</script>"
    crafted = WorkspaceManifest(
        version=1,
        entities=build(finance).manifest.entities,
        snapshots=(),
        knowledge=(
            KnowledgeManifestEntry(
                context_id=finance["context"].id,
                relative_path=f"contexts/{hostile}/overview.md",
                content_hash="0" * 64,
            ),
        ),
    )

    result = verify_workspace_manifest(session, store, crafted)

    assert result.unchanged is False
    assert hostile not in result.describe()
    assert any(change.actual == "foreign_path" for change in result.changes), result.changes
    assert not any(
        hostile in getattr(change, name)
        for change in result.changes
        for name in change.__dataclass_fields__
    )
    # The refusal reason is a fixed vocabulary, not the recorded address.
    assert {change.actual for change in result.changes} <= {
        "context_id",
        "invalid_path",
        "foreign_path",
        "context_unbound",
        "no_manifest_context_path",
    }


# ---------------------------------------------------------------------------
# D1 -- the topic scan pays for every entry it looks at
# ---------------------------------------------------------------------------


def test_list_topic_files_counts_every_entry_and_lstats_only_what_can_become_a_stem(
    store, monkeypatch
):
    """The scan bound is a *work* bound, so it counts entries examined, not entries kept.

    A directory that is mostly non-markdown used to be walked to the end when the filter
    ran before the counter, and each candidate cost an ``lstat`` whether or not it could
    still become a stem. Both are exactly the work the bound exists to prevent. The
    enumeration is fed in a fixed order here so the window is deterministic; what is
    asserted is how much was examined and statted, not which names happen to be found.
    """
    directory = store.root / "contexts" / "c1" / "topics"
    directory.mkdir(parents=True)
    # "m" sorts before the noise, so the markdown files are inside the scanned window and
    # the early stop cannot make this test pass or fail by readdir order.
    markdown = [f"m{index:02d}.md" for index in range(5)]
    for name in markdown:
        (directory / name).write_text("x", encoding="utf-8")
    noise = MAX_TOPIC_FILE_SCAN + 500
    for index in range(noise):
        (directory / f"n{index:05d}.txt").write_text("x", encoding="utf-8")

    examined: list[str] = []
    statted: list[str] = []
    original_scandir = os.scandir

    class entry:
        """A directory entry that reports its own ``stat`` calls."""

        def __init__(self, wrapped):
            self._wrapped = wrapped
            self.name = wrapped.name

        def is_symlink(self):
            return self._wrapped.is_symlink()

        def stat(self, *, follow_symlinks=True):
            statted.append(self.name)
            return self._wrapped.stat(follow_symlinks=follow_symlinks)

    class sorted_entries:
        """``scandir`` replacement that yields in name order and counts what it yields."""

        def __init__(self, path):
            self._path = path

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def __iter__(self):
            with original_scandir(self._path) as handle:
                for wrapped in sorted(handle, key=lambda item: item.name):
                    examined.append(wrapped.name)
                    yield entry(wrapped)

    monkeypatch.setattr(os, "scandir", sorted_entries)
    stems = store.list_topic_files("contexts/c1", limit=3)

    assert stems == ("m00", "m01", "m02")
    # The scan stopped at the bound instead of walking the directory to its end. A ``for``
    # loop would already have pulled entry N+1 by the time its body could break, so the
    # exact figure is part of the guarantee, not a rounding artefact.
    assert len(examined) == MAX_TOPIC_FILE_SCAN
    assert len(examined) < len(markdown) + noise
    # An ``lstat`` was paid only for the entries that could still become a stem: five
    # calls for two thousand examined entries.
    assert statted == markdown

    # A directory without a single markdown candidate costs no ``lstat`` at all.
    other = store.root / "contexts" / "c2" / "topics"
    other.mkdir(parents=True)
    for index in range(50):
        (other / f"n{index:05d}.txt").write_text("x", encoding="utf-8")
    examined.clear()
    statted.clear()

    assert store.list_topic_files("contexts/c2", limit=3) == ()
    assert statted == []
    assert len(examined) == 50
