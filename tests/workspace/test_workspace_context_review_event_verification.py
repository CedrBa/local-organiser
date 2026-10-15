"""Event verification contracts for workspace."""

from __future__ import annotations

from sqlalchemy import text
from organiser.db.models import Event
from organiser.domain.workspace_contracts import WorkspaceManifest
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    DEFAULT_WORKSPACE_POLICY,
    WorkspacePolicy,
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import BASE, add_context, add_event, workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# D2 -- a stored JSON null is a loaded payload, not a withheld one
# ---------------------------------------------------------------------------


def test_a_stored_json_null_is_loaded_and_not_reported_as_withheld(workspace_env):
    """``None`` is what "loaded" and "not loaded" both look like from the decoded value.

    Such a row holds the four characters ``null``, and SQLite measures four -- it is well
    within any read bound. Deciding from the decoded value instead ("it is ``None``,
    therefore it was not loaded") announces a payload that *was* read as withheld, puts
    it into ``omitted_counts``, and records an empty digest the verifier then has nothing
    to compare. The budget decision therefore travels as a SQL column beside the measured
    length, and this pins "loaded" apart from "absent".
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="JSON null")
    session.add(
        Event(
            occurred_at=BASE,
            event_type="OHNE_INHALT",
            entity_type="context",
            entity_id=context.id,
            payload=None,
        )
    )
    session.commit()

    packet = workspace_context(session, context.id, store)

    event = packet.recent_history[-1]
    assert event.event_type == "OHNE_INHALT"
    assert event.payload is None
    assert event.payload_truncated is False, "eine geladene Payload wurde als ausgelassen gemeldet"
    assert "event_payloads" not in packet.selection.omitted_counts
    recorded = event_entry(packet, "OHNE_INHALT")
    assert recorded.selected_fields["payload_length"] == len("null")
    assert recorded.selected_fields["payload_hash"], (
        "eine geladene Payload wurde ohne Digest verzeichnet"
    )
    # The verifier reproduces that digest, so the packet verifies itself.
    assert verify_workspace_manifest(session, store, packet.manifest).unchanged is True

    # A row with no payload *at all* (SQL NULL) is the other "absent" form and must be
    # treated the same way: length zero, loaded, never counted as a withheld payload.
    session.execute(text("UPDATE events SET payload = NULL WHERE event_type = 'OHNE_INHALT'"))
    session.commit()

    packet = workspace_context(session, context.id, store)

    event = packet.recent_history[-1]
    assert event.payload is None and event.payload_truncated is False
    assert "event_payloads" not in packet.selection.omitted_counts
    recorded = event_entry(packet, "OHNE_INHALT")
    assert recorded.selected_fields["payload_length"] == 0
    assert recorded.selected_fields["payload_hash"]


# ---------------------------------------------------------------------------
# D3 -- the verifier reads event columns, never a full ORM entity
# ---------------------------------------------------------------------------


def test_the_verifier_does_not_reach_a_payload_through_the_identity_map(counted_session):
    """``session.get(Event, ...)`` returns whatever the identity map already holds.

    The verifier re-read events as ORM objects, so the very request meant to prove the new
    read bound could be handed a multi-megabyte blob that an earlier build in the same
    session had already loaded -- and the identity map is precisely what keeps it. A
    column query never consults that map and defers nothing, so nothing can pull the blob
    behind the bound's back. The counter is the work, not a stopwatch.
    """
    session, seen = counted_session
    context = add_context(session, title="Verifizierung ohne ORM-Entity")
    event = add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="PRUEFUNG",
        payload={"note": "x" * 4_000},
    )
    packet = workspace_context(session, context.id, None)
    recorded = event_entry(packet, "PRUEFUNG")

    # Prime the identity map with the complete row, then count from here on.
    cached = session.get(Event, event.id)
    assert cached is not None and len(cached.payload["note"]) == 4_000
    assert seen, "der Zeichenzaehler ist nicht verdrahtet"
    seen.clear()

    # A forged manifest claiming the packet was built with a cap of zero. The verifier has
    # to honour that cap, read nothing at all, and say that it could not reproduce the
    # digest instead of quietly passing or inventing a content change.
    forged = recorded.model_copy(
        update={"selected_fields": {**recorded.selected_fields, "payload_read_cap": 0}}
    )
    result = verify_workspace_manifest(
        session, None, WorkspaceManifest(version=1, entities=(forged,), snapshots=(), knowledge=())
    )

    assert seen == [], "der Verifier hat eine Event-Payload gelesen"
    assert result.unchanged is False
    assert any(
        change.field == "payload_hash" and change.actual == "payload_beyond_verifier_cap"
        for change in result.changes
    ), result.changes
    # The measured length is what the verifier may still check.
    assert not any(change.field == "payload_length" for change in result.changes)


# ---------------------------------------------------------------------------
# D4 -- the payload cap is an input of the verification, and it can only narrow
# ---------------------------------------------------------------------------


def test_a_differing_payload_cap_does_not_make_a_packet_permanently_stale(workspace_env):
    """The cap the packet used decides what it hashed; the caller's cap decides what may
    be read. Comparing the two would report a change for every legitimate policy
    difference -- the permanent stale this workstream had to undo once already.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Enge Lesegrenze")
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="SCHMAL",
        payload={"note": "y" * 400},
    )
    narrow = WorkspacePolicy(max_event_payload_bytes=200)

    packet = workspace_context(session, context.id, store, narrow)
    recorded = event_entry(packet, "SCHMAL")
    assert recorded.selected_fields["payload_read_cap"] == 200
    assert recorded.selected_fields["payload_hash"] == ""
    assert recorded.selected_fields["payload_length"] > 200

    # Verified with the *default* policy, i.e. one that is wider than the recorded one:
    # the recorded empty digest must not turn into a reported content change.
    assert verify_workspace_manifest(session, store, packet.manifest).unchanged is True
    assert verify_workspace_manifest(session, store, packet.manifest, narrow).unchanged is True

    # The other direction: a digest recorded under a wide cap that this verification may
    # not reproduce is reported explicitly, fail-closed, instead of being blanked out.
    wide = workspace_context(session, context.id, store)
    result = verify_workspace_manifest(session, store, wide.manifest, narrow)

    assert result.unchanged is False
    assert any(
        change.field == "payload_hash" and change.actual == "payload_beyond_verifier_cap"
        for change in result.changes
    ), result.changes


def test_a_forged_payload_read_cap_cannot_raise_the_verifiers_own_bound(counted_session):
    """Neither the recorded cap nor an unchecked policy may raise what is read.

    The recorded cap is an input, so it is bounded against the caller's *clamped* policy:
    a hand-built manifest claiming ten gigabytes buys nothing, and the work stays inside
    the caller's own limit.
    """
    session, seen = counted_session
    context = add_context(session, title="Gefaelschte Lesegrenze")
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="RIESIG",
        payload={"note": "z" * 4_000},
    )
    packet = workspace_context(session, context.id, None)
    recorded = event_entry(packet, "RIESIG")
    forged = recorded.model_copy(
        update={"selected_fields": {**recorded.selected_fields, "payload_read_cap": 10**9}}
    )
    seen.clear()

    result = verify_workspace_manifest(
        session, None, WorkspaceManifest(version=1, entities=(forged,), snapshots=(), knowledge=())
    )

    policy = DEFAULT_WORKSPACE_POLICY.clamped()
    assert seen, "der Verifier hat gar nichts gelesen -- der Test ist nicht verdrahtet"
    assert sum(seen) <= policy.max_event_payload_bytes
    assert result.unchanged is True, result.describe()
