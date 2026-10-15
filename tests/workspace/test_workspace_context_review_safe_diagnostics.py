"""Safe diagnostics contracts for workspace."""

from __future__ import annotations

import pytest
from organiser.domain.knowledge_path import MAX_KNOWLEDGE_PATH_LENGTH
from organiser.domain.workspace_contracts import KnowledgeManifestEntry
from organiser.services.knowledge import (
    KnowledgeStore,
)
from organiser.services.workspace_context import (
    verify_workspace_manifest,
    workspace_context,
)
from support.workspace import (
    add_context,
    add_event,
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


# ---------------------------------------------------------------------------
# D5 -- the machine reference is exact, the human line is bounded
# ---------------------------------------------------------------------------


def test_a_long_machine_reference_stays_exact_while_the_human_line_stays_bounded(workspace_env):
    """A caller maps a change back to its row through ``reference``.

    A legal knowledge address is up to 512 characters plus the store file name, so cutting
    the machine value at the 300 characters meant for a diagnostic line produced a
    reference that matches no file and no row -- worse than saying nothing at all. The
    machine value is therefore kept exact up to the store's own path bound, and only
    ``describe()`` is bounded.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    knowledge_path = "/".join(["kurse"] + [f"abschnitt{index:02d}" for index in range(40)])
    assert len(knowledge_path) <= MAX_KNOWLEDGE_PATH_LENGTH
    context = add_context(session, title="Langer Pfad", path=knowledge_path)
    session.commit()
    write_knowledge(store, knowledge_path, "overview.md", "# Ueberblick\n\nStand.\n")

    packet = workspace_context(session, context.id, store)

    recorded = packet.manifest.knowledge[0].relative_path
    assert recorded == f"{knowledge_path}/overview.md"
    assert len(recorded) > 300
    (store.root / recorded).write_text("# Von Hand geaendert\n", encoding="utf-8")

    result = verify_workspace_manifest(session, store, packet.manifest)

    change = next(item for item in result.changes if item.kind == "knowledge")
    assert change.reference == recorded, "die Maschinenreferenz wurde gekuerzt oder umgeschrieben"
    # The human-readable line stays inside its own bound and names the same value.
    assert recorded not in result.describe()
    assert recorded[:200] in result.describe()
    assert len(result.describe()) < 500


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("contexts/</script><script>alert(1)</script>/overview.md", id="markup"),
        pytest.param("contexts/mit\nZeilenumbruch/overview.md", id="control"),
        pytest.param("/home/operator/GEHEIM/overview.md", id="absolute"),
        # Relative in front, absolute behind: the address is not at the start of the
        # string, and a check that only looked at the first character called this safe.
        pytest.param("kurse/plan=/home/operator/GEHEIM/overview.md", id="embedded-absolute"),
        pytest.param(f"kurse/{'y' * 600}/overview.md", id="overlong"),
    ],
)
def test_an_unsafe_machine_reference_is_refused_never_rewritten(finance, hostile):
    """Refusing is the only safe answer for something that is not a machine reference.

    Rewriting a hostile or over-long address into a plausible reference would be worse
    than the attack it prevents: the caller would look for a row that cannot exist. So the
    reference is replaced by a short fixed marker, the *reason* stays in the fixed
    vocabulary of ``actual``, and nothing the caller supplied reaches an error body.
    """
    session, store = finance["session"], finance["store"]
    crafted = build(finance).manifest.model_copy(
        update={
            "knowledge": (
                KnowledgeManifestEntry(
                    context_id=finance["context"].id, relative_path=hostile, content_hash="0" * 64
                ),
            )
        }
    )

    result = verify_workspace_manifest(session, store, crafted)

    assert result.unchanged is False
    change = next(item for item in result.changes if item.kind == "knowledge")
    assert hostile not in change.reference
    assert "<" not in change.reference and ">" not in change.reference
    assert len(change.reference) <= 64, (
        "die Referenz ist nicht durch ein festes Markerwort begrenzt"
    )
    assert hostile not in result.describe()
    assert hostile not in change.field + change.expected + change.actual
    assert change.actual in {
        "invalid_path",
        "foreign_path",
        "context_unbound",
        "no_manifest_context_path",
    }, change.actual
    assert "0" * 64 not in result.describe(), "eine abgewiesene Adresse wurde doch gehasht"


def test_a_host_address_anywhere_in_an_event_payload_never_reaches_the_packet(workspace_env):
    """The redaction looked at the first character of a value and at no key at all.

    The packet promises no host addresses, and ``recent_history`` is read by the very
    prompts the excerpts feed. A legacy ``CONTEXT_SOURCE_ADDED`` payload does not store
    its path as the whole value -- it stores a sentence, a ``key=/path`` assignment and
    a *key* that is itself the path. "Starts with a separator" passed all three through
    untouched, so the guarantee held only for the shape that had been thought of.

    The assertion is on the whole serialized packet, not on one field: a leak through a
    key, a nested list or the manifest would otherwise go unnoticed.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Adresse ueberall")
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="CONTEXT_SOURCE_ADDED",
        payload={
            "message": "Backup unter /home/operator/GEHEIM/plan.pdf abgelegt",
            "plan=/home/operator/GEHEIM/zwei.pdf": "zweiter Schluessel",
            "windows": "C:\\Kurs\\vorlesung.pdf wurde geprueft",
            "unc": "\\\\Fileserver\\Kurs\\vorlesung.pdf",
            "locator": "file:///home/operator/GEHEIM/drei.pdf",
            "hidden": "siehe /geheim",
            "nested": {"pfad": ["/var/lib/GEHEIM/vier.pdf", 7]},
        },
    )

    packet = workspace_context(session, context.id, store)

    rendered = packet.model_dump_json()
    assert_no_host_address(rendered)
    assert "GEHEIM" not in rendered, "auch der Name des Verzeichnisses blieb stehen"
    event = next(
        item for item in packet.recent_history if item.event_type == "CONTEXT_SOURCE_ADDED"
    )
    payload = event.payload
    # Only the address is withheld: the sentence around it still says what happened,
    # because that is the provenance the event exists for.
    assert payload["message"] == "Backup unter <absolute Pfad ausgelassen> abgelegt"
    assert payload["windows"] == "<absolute Pfad ausgelassen> wurde geprueft"
    assert payload["nested"]["pfad"][0] == "<absolute Pfad ausgelassen>"
    # The path-shaped *key* is withheld as well; its value stays, so the entry is still
    # countable and the omission is visible instead of silent.
    assert "<absolute Pfad ausgelassen>" in payload
    assert payload["<absolute Pfad ausgelassen>"] == "zweiter Schluessel"
    assert payload["nested"]["pfad"][1] == 7, "unbeteiligte Werte wurden mitgeschrediert"
    assert event.payload_truncated is True


def test_a_value_with_ordinary_slashes_survives_the_payload_projection(workspace_env):
    """The counter-test for the rule above: no slash is an address.

    A payload is prose. ``3/4``, a date, a module path and a store-relative address all
    contain separators, and a rule that redacted every one of them would make the event
    unreadable without making it safer. What tells them apart is that none of them
    *begins* with a separator -- which is the entire rule.
    """
    session, tmp_path = workspace_env
    root = tmp_path / "knowledge"
    root.mkdir()
    store = KnowledgeStore(root)
    context = add_context(session, title="Gewöhnliche Schrägstriche")
    add_event(
        session,
        entity_type="context",
        entity_id=context.id,
        event_type="NOTIZ",
        payload={
            "anteil": "3/4 ab 2026/01/02",
            "modul": "src/organiser/services/workspace_context.py",
            "adresse": "kurse/teil 1/overview.md",
            "trennung": "und/oder",
            "pfad_feld": "progress.md",
        },
    )

    packet = workspace_context(session, context.id, store)

    payload = next(item for item in packet.recent_history if item.event_type == "NOTIZ").payload
    assert payload == {
        "anteil": "3/4 ab 2026/01/02",
        "modul": "src/organiser/services/workspace_context.py",
        "adresse": "kurse/teil 1/overview.md",
        "trennung": "und/oder",
        "pfad_feld": "progress.md",
    }, "ein Wert ohne Hostadresse wurde veraendert"
    assert (
        next(item for item in packet.recent_history if item.event_type == "NOTIZ").payload_truncated
        is False
    )
