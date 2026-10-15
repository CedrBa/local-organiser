"""Binding verification contracts for workspace."""

from __future__ import annotations

import pytest
from organiser.domain.workspace_contracts import KnowledgeManifestEntry, WorkspaceManifest
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import add_context, initialise_knowledge, workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# F2 -- a knowledge binding is not necessarily ``contexts/<id>``
# ---------------------------------------------------------------------------


def test_a_custom_knowledge_binding_verifies_unchanged(workspace_env):
    """``contexts/<id>/`` was hardcoded, so a valid custom binding never verified.

    ``create_context`` accepts any canonical store-relative path, and
    ``kurse/finanzierung`` is exactly the kind of name a user binds on purpose. Its
    packet was correct -- the builder read it -- but the apply gate was permanently
    closed, because the verifier called such an address "foreign".
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Eigener Pfad", path="kurse/finanzierung")
    session.commit()
    initialise_knowledge(store, "kurse/finanzierung")

    packet = workspace_context(session, context.id, store)

    assert [excerpt.source_id for excerpt in packet.knowledge_excerpts] == [
        "kurse/finanzierung/overview.md",
        "kurse/finanzierung/progress.md",
        "kurse/finanzierung/notes.md",
    ]
    assert {entry.relative_path for entry in packet.manifest.knowledge} == {
        "kurse/finanzierung/overview.md",
        "kurse/finanzierung/progress.md",
        "kurse/finanzierung/notes.md",
    }
    result = verify_workspace_manifest(session, store, packet.manifest)
    assert result.unchanged is True, result.describe()


def test_a_hand_edited_custom_binding_still_fails_closed(workspace_env):
    """The relaxed prefix must not relax the hash comparison itself."""
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Eigener Pfad", path="kurse/finanzierung")
    session.commit()
    initialise_knowledge(store, "kurse/finanzierung")
    packet = workspace_context(session, context.id, store)

    (root / "kurse/finanzierung" / "overview.md").write_text("manipuliert\n", encoding="utf-8")

    result = verify_workspace_manifest(session, store, packet.manifest)
    assert result.unchanged is False
    assert any(change.field == "content_hash" for change in result.changes)


def test_a_rebound_context_stops_verifying_its_old_knowledge(workspace_env):
    """The *current* binding is the authoritative one.

    Both recorded values come from the same untrusted manifest, so neither may be the
    only anchor: a context that was rebound to another valid path after the packet was
    built must not have its old markdown hashed, or the comparison would be an oracle
    over a directory that no longer belongs to it.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Umgebuendert", path="kurse/alt")
    session.commit()
    initialise_knowledge(store, "kurse/alt")
    packet = workspace_context(session, context.id, store)

    context.knowledge_path = "kurse/neu"
    session.commit()

    result = verify_workspace_manifest(session, store, packet.manifest)
    assert result.unchanged is False

    # The context *entity* legitimately reports its own rebinding -- ``knowledge_path``
    # is one of its compared fields and it genuinely changed. That is not what this test
    # is about, and asserting on the field name alone would let the entity change stand
    # in for the refusal: deleting the current-binding check entirely would leave this
    # test green while the old markdown got hashed again. So the assertion is on the
    # *kind*, which is what distinguishes "the binding moved" from "this address may not
    # be read", and every recorded file has to be among them.
    refused = [change for change in result.changes if change.kind == "knowledge"]
    assert refused, result.changes
    assert {change.actual for change in refused} == {"foreign_path"}
    assert {change.reference for change in refused} == {
        "kurse/alt/overview.md",
        "kurse/alt/progress.md",
        "kurse/alt/notes.md",
    }
    # Nothing under the old binding was hashed to reach that verdict.
    assert "content_hash" not in {change.field for change in result.changes}


@pytest.mark.parametrize(
    "address",
    [
        "contexts/ein-anderer-context/overview.md",
        "kurse/finanzierung/../../ausserhalb.md",
        "/etc/passwd",
        "kurse/finanzierung",
        "kurse/finanzierun/overview.md",
    ],
)
def test_an_address_outside_the_binding_is_refused(finance, address):
    """Sibling, traversal, absolute and near-miss addresses all stay fail-closed."""
    session, store = finance["session"], finance["store"]
    crafted = WorkspaceManifest(
        version=1,
        entities=build(finance).manifest.entities,
        snapshots=(),
        knowledge=(
            KnowledgeManifestEntry(
                context_id=finance["context"].id, relative_path=address, content_hash="0" * 64
            ),
        ),
    )

    result = verify_workspace_manifest(session, store, crafted)

    assert result.unchanged is False, address
    # ``kind`` rather than ``field`` alone: ``knowledge_path`` is also a compared field of
    # the context *entity*, so a field-only assertion could be satisfied by an unrelated
    # entity change and would not prove this address was refused.
    refused = [change for change in result.changes if change.kind == "knowledge"]
    assert refused, (address, result.changes)
    assert all(change.field == "knowledge_path" for change in refused), (address, result.changes)
    assert "content_hash" not in {change.field for change in result.changes}
    assert "0" * 64 not in result.describe()


def test_an_unbound_context_refuses_every_knowledge_address(finance):
    """No current binding means no address may be read -- the reader may not invent one."""
    session, store = finance["session"], finance["store"]
    manifest = build(finance).manifest
    assert manifest.knowledge, "der Test braucht einen Aufbau mit Knowledge"

    session.get(type(finance["context"]), finance["context"].id).knowledge_path = None
    session.commit()

    result = verify_workspace_manifest(session, store, manifest)

    assert result.unchanged is False
    assert any(change.actual == "context_unbound" for change in result.changes)
