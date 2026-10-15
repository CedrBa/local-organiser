"""Transaction ownership contracts for context_knowledge."""

from __future__ import annotations

import pytest
from sqlalchemy import select, text
from organiser.db.models import Project
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    KNOWLEDGE_INIT_COMPLETED_EVENT,
    ContextKnowledgeError,
)
from organiser.services.domain import (
    create_context,
)
from support.context_knowledge_lifecycle_regressions import (
    MANUAL_PATH as MANUAL_PATH,
    session as session,
    service as service,
    knowledge_events as knowledge_events,
    event_types as event_types,
    tree as tree,
    stored_path as stored_path,
    stored_project_title as stored_project_title,
    make_project as make_project,
    raise_filesystem as raise_filesystem,
    sibling_session as sibling_session,
    stale_session as stale_session,
    other_session as other_session,
    context_with_knowledge as context_with_knowledge,
)


# -- 1. Transaktionsgrenze ----------------------------------------------------


def test_flushed_foreign_insert_is_neither_committed_nor_rolled_back(
    session, service, knowledge_root, other_session
):
    """Ein geflushter fremder INSERT darf vom Service nicht mitcommittet werden.

    Am Ausgangsstand sieht ``session.new`` nur *ungeflushte* Objekte. Nach
    ``flush()`` committete ``ensure_context_knowledge`` die fremde Zeile mit.
    """
    created = create_context(session, title="SAP", type="application")
    foreign = make_project(session, title="Fremdes Projekt", commit=False)

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    # Weder committet ...
    assert stored_path(other_session, created.id) is None
    # ... noch verworfen: der Aufrufer kann seine eigene Arbeit beenden.
    session.commit()
    assert stored_project_title(other_session, foreign.id) == "Fremdes Projekt"
    assert tree(knowledge_root) == set()


def test_flushed_foreign_update_is_neither_committed_nor_rolled_back(
    session, service, knowledge_root, other_session
):
    created = create_context(session, title="SAP", type="application")
    foreign = make_project(session, title="Fremdes Projekt")
    foreign.title = "Umbenannt"
    session.flush()

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    assert stored_project_title(other_session, foreign.id) == "Fremdes Projekt"
    assert tree(knowledge_root) == set()

    session.commit()
    assert stored_project_title(other_session, foreign.id) == "Umbenannt"


def test_flushed_foreign_delete_is_neither_committed_nor_rolled_back(
    session, service, knowledge_root, other_session
):
    created = create_context(session, title="SAP", type="application")
    foreign = make_project(session, title="Fremdes Projekt")
    session.delete(foreign)
    session.flush()

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    assert stored_project_title(other_session, foreign.id) == "Fremdes Projekt"
    assert tree(knowledge_root) == set()


def test_session_execute_write_is_not_committed_by_apply(
    session, service, other_session, context_with_knowledge
):
    """Auch rohe ``session.execute``-Schreibvorgaenge sind fremde Arbeit."""
    created = context_with_knowledge
    foreign = make_project(session, title="Fremdes Projekt")
    session.execute(
        text("UPDATE projects SET title = 'Direkt geaendert' WHERE id = :id"), {"id": foreign.id}
    )

    with pytest.raises(ContextKnowledgeError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Zu frueh.")
        )

    assert stored_project_title(other_session, foreign.id) == "Fremdes Projekt"

    session.commit()
    assert stored_project_title(other_session, foreign.id) == "Direkt geaendert"


def test_apply_refuses_a_caller_transaction(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    make_project(session, title="Fremdes Projekt", commit=False)  # offene Caller-Transaktion

    with pytest.raises(ContextKnowledgeError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Zu frueh.")
        )

    assert "Zu frueh." not in (knowledge_root / f"contexts/{created.id}" / "notes.md").read_text(
        encoding="utf-8"
    )
    session.rollback()


def test_refused_call_leaves_the_caller_transaction_alone(session, service):
    """Eine Ablehnung arbeitet nicht in der Transaktion des Aufrufers."""
    created = create_context(session, title="SAP", type="application")
    make_project(session, title="Fremdes Projekt", commit=False)

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    # Der Service hat nichts angefasst: die Transaktion gehoert weiter dem Aufrufer.
    assert session.in_transaction() is True
    assert len(session.execute(select(Project)).scalars().all()) == 1
    session.rollback()


def test_failed_ensure_closes_its_own_transaction(session, service, monkeypatch):
    """Nach einem Fehler bleibt keine offene Service-Transaktion zurueck."""
    created = create_context(session, title="SAP", type="application")
    context_id = created.id
    monkeypatch.setattr(service._store, "ensure_context_knowledge", raise_filesystem, raising=True)

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(context_id)

    assert session.in_transaction() is False
    # Der Aufrufer kann die Transaktion normal beenden und den Vorgang wiederholen.
    session.rollback()
    monkeypatch.undo()
    repaired = service.ensure_context_knowledge(context_id)
    assert repaired.changed is True
    assert event_types(session, context_id)[-1] == KNOWLEDGE_INIT_COMPLETED_EVENT
