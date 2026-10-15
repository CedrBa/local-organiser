"""Outcomes contracts for context_knowledge."""

from __future__ import annotations

import os
import stat
import pytest
from organiser.db.models import Context
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    KNOWLEDGE_INIT_FAILED_EVENT,
    KNOWLEDGE_WRITE_COMPLETED_EVENT,
    KNOWLEDGE_WRITE_FAILED_EVENT,
    KNOWLEDGE_WRITE_INTENT_EVENT,
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeError,
)
from organiser.services.domain import (
    create_context,
    get_context,
    list_contexts,
)
from support.context_knowledge_integration import (
    alembic_config as alembic_config,
    session as session,
    service as service,
    event_types as event_types,
    tree as tree,
    _raise_commit as _raise_commit,
    _raise_filesystem as _raise_filesystem,
    _insert_context as _insert_context,
)


def test_ensure_never_creates_a_file_for_a_context_that_was_not_committed(
    session, service, knowledge_root
):
    """Nicht committete Contexts bekommen keinen Wissensspeicher.

    Der Vertrag ist strenger als "keine Datei": weil der Service eine aktive
    Caller-Transaktion ablehnt, wird ein geflushter Entwurf schon am Eintritt
    zurueckgewiesen -- er kann gar nicht erst gebunden werden.
    """
    pending = Context(title="Entwurf", type="topic")
    session.add(pending)
    session.flush()

    with pytest.raises(ContextKnowledgeError) as failure:
        service.ensure_context_knowledge(pending.id)

    assert "Transaktion" in str(failure.value)
    assert tree(knowledge_root) == set()

    # Die fremde Arbeit bleibt beim Aufrufer: nicht committet, nicht verworfen.
    session.rollback()
    assert tree(knowledge_root) == set()
    assert list_contexts(session) == []


# -- failure and recovery -----------------------------------------------------


def test_database_error_before_write_leaves_the_filesystem_untouched(
    session, service, knowledge_root, monkeypatch
):
    created = create_context(session, title="SAP", type="application")
    monkeypatch.setattr(service, "_commit", _raise_commit)

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    assert tree(knowledge_root) == set()
    session.expire_all()
    assert get_context(session, created.id).knowledge_path is None


def test_filesystem_error_after_binding_keeps_the_context_valid_and_repairable(
    session, service, knowledge_store, knowledge_root, monkeypatch
):
    created = create_context(session, title="SAP", type="application")
    context_id = created.id
    monkeypatch.setattr(
        knowledge_store, "ensure_context_knowledge", _raise_filesystem, raising=True
    )

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(context_id)

    session.expire_all()
    assert get_context(session, context_id).knowledge_path == f"contexts/{context_id}"
    assert get_context(session, context_id).status == "active"
    assert event_types(session, context_id)[-1] == KNOWLEDGE_INIT_FAILED_EVENT
    assert tree(knowledge_root) == set()

    # Der Aufrufer beendet seine eigene Transaktion (der Service hat seine bereits
    # sauber geschlossen) und versucht es erneut.
    session.rollback()
    monkeypatch.undo()
    repaired = service.ensure_context_knowledge(context_id)
    assert repaired.changed is True
    assert (knowledge_root / repaired.knowledge_path / "overview.md").is_file()


def test_successful_write_with_failing_final_commit_leaves_an_unresolved_audit_state(
    session, service, knowledge_root, monkeypatch
):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    path = f"contexts/{created.id}"
    overview = knowledge_root / path / "overview.md"
    before = overview.read_text(encoding="utf-8")

    # Der Intent-Commit gelingt, der abschliessende Commit des Erfolgsereignisses nicht.
    real_commit = service._commit
    calls = {"count": 0}

    def flaky_commit():
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("Datenbank nicht erreichbar")
        return real_commit()

    monkeypatch.setattr(service, "_commit", flaky_commit)

    with pytest.raises(ContextKnowledgeAmbiguousError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="update_overview", text="Neuer Stand."
            )
        )

    # Datei ist geschrieben, wird aber nicht zurueckgerollt.
    assert "Neuer Stand." in overview.read_text(encoding="utf-8")
    assert overview.read_text(encoding="utf-8") != before
    # Audit: Intent vorhanden, kein Abschluss. Kein Erfolg behauptet.
    assert event_types(session, created.id)[-1] == KNOWLEDGE_WRITE_INTENT_EVENT
    assert KNOWLEDGE_WRITE_COMPLETED_EVENT not in event_types(session, created.id)


def test_non_idempotent_note_is_not_retried_automatically_after_an_ambiguous_commit(
    session, service, knowledge_root, monkeypatch
):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    notes = knowledge_root / f"contexts/{created.id}" / "notes.md"
    real_commit = service._commit
    calls = {"count": 0}

    def flaky_commit():
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("Datenbank nicht erreichbar")
        return real_commit()

    monkeypatch.setattr(service, "_commit", flaky_commit)
    with pytest.raises(ContextKnowledgeAmbiguousError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Einmalig.")
        )

    assert notes.read_text(encoding="utf-8").count("Einmalig.") == 1


def test_filesystem_error_during_apply_records_a_failure_and_no_success(
    session, service, knowledge_root, monkeypatch
):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    monkeypatch.setattr(service._store, "update_overview", _raise_filesystem, raising=True)

    with pytest.raises(ContextKnowledgeError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="Text")
        )

    assert event_types(session, created.id)[-1] == KNOWLEDGE_WRITE_FAILED_EVENT
    assert KNOWLEDGE_WRITE_COMPLETED_EVENT not in event_types(session, created.id)
    assert "Text" not in (knowledge_root / f"contexts/{created.id}" / "overview.md").read_text(
        encoding="utf-8"
    )


def test_write_to_a_readonly_directory_fails_cleanly(session, service, knowledge_root, monkeypatch):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    original = os.replace

    def failing_replace(source, target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", failing_replace)

    with pytest.raises(ContextKnowledgeError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="append_note", text="Nicht geschrieben."
            )
        )

    assert event_types(session, created.id)[-1] == KNOWLEDGE_WRITE_FAILED_EVENT
    assert "Nicht geschrieben." not in (
        knowledge_root / f"contexts/{created.id}" / "notes.md"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(os, "replace", original)
    assert stat.S_ISDIR((knowledge_root / f"contexts/{created.id}").stat().st_mode)
