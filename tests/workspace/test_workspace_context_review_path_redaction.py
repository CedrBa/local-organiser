"""Path redaction contracts for workspace."""

from __future__ import annotations

from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    _ABSOLUTE_PATH_MARKER,
    workspace_context,
)
from support.workspace import add_context, add_event, workspace_env as workspace_env
from support.workspace import finance as finance, store as store
from support.workspace_context_review import (
    build as build,
    event_entry as event_entry,
    counted_session as counted_session,
    HOST_ADDRESSES as HOST_ADDRESSES,
    assert_no_host_address as assert_no_host_address,
)


# ---------------------------------------------------------------------------
# UNC and Complex Path Redaction Tests
# ---------------------------------------------------------------------------


def test_unc_and_complex_paths_in_event_payloads_are_redacted_across_full_packet(workspace_env):
    """Embedded UNC paths with two leading backslashes in longer sentences, as well as
    POSIX and Windows paths containing spaces, '=', and punctuation, must be fully
    redacted across the entire serialized packet (model_dump_json).
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Pfad Redaktion")

    unc_sentence = (
        r"Die Projektablage liegt auf \\fileserver01\projects\team alpha\q1=2026\summary.pdf "
        r"und wurde zur Pruefung abgelegt."
    )
    posix_path = r"/var/data/shared reports/project=beta/results;final.csv"
    win_path = r"C:\Program Files\Finance App\config=production\settings.xml"
    unc_spaces_eq = r"\\nas-share\team finance\dept=controlling\budget 2026=approved.xlsx"

    payload = {
        "unc_in_sentence": unc_sentence,
        "posix_complex": posix_path,
        "win_complex": win_path,
        "unc_complex": unc_spaces_eq,
        # Path also as a dictionary key:
        r"\\backup-server\share\file.bak": "val",
    }
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="CUSTOM_EVENT",
        payload=payload,
    )
    session.commit()

    packet = workspace_context(session, context.id, store)
    serialized = packet.model_dump_json()

    # Raw paths must NOT appear anywhere in the serialized packet
    assert r"\\fileserver01\projects" not in serialized
    assert r"\\nas-share\team finance" not in serialized
    assert r"\\backup-server\share" not in serialized
    assert r"/var/data/shared reports" not in serialized
    assert r"C:\Program Files\Finance App" not in serialized

    # Redaction marker must appear in the packet
    assert _ABSOLUTE_PATH_MARKER in serialized

    # Surrounding prose in the sentence must survive
    assert "Die Projektablage liegt auf" in serialized
    assert "und wurde zur Pruefung abgelegt." in serialized
