"""Knowledge boundaries contracts for workspace."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from organiser.services.knowledge import (
    KnowledgeStore,
    UnsafeKnowledgePathError,
)
from organiser.services.workspace_context import (
    _INVALID_KNOWLEDGE_PATH_MARKER,
    DEFAULT_WORKSPACE_POLICY,
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import add_context, write_knowledge, workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# E2 -- an empty result means "absent", and only that
# ---------------------------------------------------------------------------


def test_a_non_directory_topics_directory_is_refused_and_never_reported_as_empty(tmp_path):
    """The docstring promised an empty result; the code refused. Only the code was right.

    ``O_DIRECTORY`` with ``O_NOFOLLOW`` cannot tell "not a directory" from "a directory
    that was swapped for one", so ``_open_below_root`` raises for both -- exactly as the
    write path ``list_topics`` always has. Returning ``()`` there would be
    indistinguishable from "this context has no legacy topic files", so the reader would
    report a complete enumeration of a directory it never saw. The contract is therefore
    documented as it behaves, and pinned here.
    """
    store = KnowledgeStore(tmp_path)

    (store.root / "contexts" / "fehlt").mkdir(parents=True)
    assert store.list_topic_files("contexts/fehlt") == (), (
        "ein fehlendes topics/ ist ein leeres Ergebnis"
    )

    (store.root / "contexts" / "datei").mkdir(parents=True)
    (store.root / "contexts" / "datei" / "topics").write_text("kein Verzeichnis", encoding="utf-8")
    with pytest.raises(UnsafeKnowledgePathError):
        store.list_topic_files("contexts/datei")

    (store.root / "contexts" / "link").mkdir(parents=True)
    (store.root / "contexts" / "link" / "echt").mkdir()
    (store.root / "contexts" / "link" / "topics").symlink_to(
        store.root / "contexts" / "link" / "echt"
    )
    with pytest.raises(UnsafeKnowledgePathError):
        store.list_topic_files("contexts/link")


def test_a_broken_topics_directory_becomes_a_visible_warning_not_a_silent_absence(
    workspace_env, tmp_path
):
    """The read model must say that it could not enumerate, not that there is nothing.

    ``list_topic_files`` refusing a non-directory is only useful if the builder turns it
    into something the user sees. An empty tuple would have produced a complete-looking
    packet that silently dropped every legacy topic file.
    """
    session, _tmp_path = workspace_env
    root = tmp_path / "knowledge"
    (root / "contexts").mkdir(parents=True)
    store = KnowledgeStore(root)
    context = add_context(session, title="Kaputtes topics", path="contexts/kaputt")
    write_knowledge(store, "contexts/kaputt", "overview.md", "# Ueberblick\n\nStand.\n")
    (root / "contexts" / "kaputt" / "topics").write_text("kein Verzeichnis", encoding="utf-8")

    packet = workspace_context(session, context.id, store)

    assert any("legacy_topics" in warning for warning in packet.warnings), packet.warnings
    # The refusal is local to the legacy enumeration: the core files still arrive.
    assert [item.label for item in packet.knowledge_excerpts] == ["context_core:overview.md"]


# ---------------------------------------------------------------------------
# E3 -- the one field that is not bounded by the policy
# ---------------------------------------------------------------------------


def test_the_knowledge_path_ceiling_comes_from_the_write_contract_not_from_the_policy(
    workspace_env,
):
    """An invalid/absolute DB knowledge_path must not leak into the packet or manifest,
    and manifest verification must be fail-closed.

    While legal relative paths are bounded by the write contract (validate_context_path,
    at most 512 characters), a row written past that contract (e.g. an absolute host
    address) is replaced with a fixed non-leaking marker in both the packet context
    summary and the manifest context entity. Manifest verification against such a row
    is fail-closed, refusing to report unchanged=True.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Vertragsbruch")
    # Deliberately bypasses ``create_context``: this row is what the write contract
    # forbids, and the question is what the read model then does with it.
    raw_forbidden_path = "/host/operator/GEHEIM/" + "x" * 600
    context.knowledge_path = raw_forbidden_path
    session.commit()
    session.expire_all()

    packet = workspace_context(session, context.id, store)

    serialized = packet.model_dump_json()
    assert raw_forbidden_path not in serialized, (
        "der rohe verbotene Pfad darf nicht im serialisierten Packet leaken"
    )
    assert packet.context.knowledge_path == _INVALID_KNOWLEDGE_PATH_MARKER
    assert (
        packet.manifest.entities[0].selected_fields["knowledge_path"]
        == _INVALID_KNOWLEDGE_PATH_MARKER
    ), "das Manifest muss denselben sicheren Marker fuehren wie das Paket"

    verification = verify_workspace_manifest(session, store, packet.manifest)
    assert verification.unchanged is False
    assert any(change.field == "knowledge_path" for change in verification.changes), (
        "Verifikation muss fail-closed sein und den ungueltigen knowledge_path beanstanden"
    )

    # The packet ceiling still holds -- it is the bound that does apply here.
    assert len(serialized) <= DEFAULT_WORKSPACE_POLICY.max_packet_chars
    # And the context is visibly broken rather than silently wrong: no knowledge excerpt
    # is claimed, and the invalid knowledge_path is warned about.
    assert packet.knowledge_excerpts == ()
    assert packet.context.knowledge_bound is True
    assert any("Ungueltiger knowledge_path" in warning for warning in packet.warnings)


def test_invalid_knowledge_path_via_raw_sql_is_redacted_and_verify_fails_closed(workspace_env):
    """A knowledge_path set via raw SQL to an absolute or invalid path must never leak
    and verification must remain fail-closed.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Raw SQL Test")
    session.commit()

    raw_path = "/var/lib/secret/store/credentials"
    session.execute(
        text("UPDATE contexts SET knowledge_path = :path WHERE id = :id"),
        {"path": raw_path, "id": context.id},
    )
    session.commit()
    session.expire_all()

    packet = workspace_context(session, context.id, store)
    serialized = packet.model_dump_json()

    assert raw_path not in serialized
    assert packet.context.knowledge_path == _INVALID_KNOWLEDGE_PATH_MARKER
    assert (
        packet.manifest.entities[0].selected_fields["knowledge_path"]
        == _INVALID_KNOWLEDGE_PATH_MARKER
    )

    verification = verify_workspace_manifest(session, store, packet.manifest)
    assert verification.unchanged is False
    assert any(
        c.reference == f"context:{context.id}" and c.field == "knowledge_path"
        for c in verification.changes
    )
