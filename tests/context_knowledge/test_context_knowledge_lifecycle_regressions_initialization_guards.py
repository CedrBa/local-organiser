"""Initialization guards contracts for context_knowledge."""

from __future__ import annotations

import pytest
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
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


# -- 3. Bindung ist keine Initialisierung -------------------------------------


def test_apply_refuses_a_manually_bound_but_uninitialized_context(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application", knowledge_path=MANUAL_PATH)

    with pytest.raises(ContextKnowledgeNotInitializedError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Zu frueh.")
        )

    # Ohne Ensure wird nicht einmal eine Datei angelegt.
    assert tree(knowledge_root) == set()
    assert event_types(session, created.id) == []
    assert stored_path(session, created.id) == MANUAL_PATH


def test_apply_refuses_a_partially_initialized_structure(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application", knowledge_path=MANUAL_PATH)
    directory = knowledge_root / "Bewerbungen" / "SAP 2026"
    directory.mkdir(parents=True)
    (directory / "notes.md").write_text("# Notizen\n", encoding="utf-8")
    (directory / "topics").mkdir()
    before = tree(knowledge_root)

    with pytest.raises(ContextKnowledgeNotInitializedError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="update_overview", text="Zu frueh."
            )
        )

    assert tree(knowledge_root) == before
    assert (directory / "notes.md").read_text(encoding="utf-8") == "# Notizen\n"
    assert event_types(session, created.id) == []


def test_apply_refuses_when_the_topics_directory_is_missing(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    (knowledge_root / f"contexts/{created.id}" / "topics").rmdir()

    with pytest.raises(ContextKnowledgeNotInitializedError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="update_overview", text="Zu frueh."
            )
        )

    assert not (knowledge_root / f"contexts/{created.id}" / "topics").exists()


def test_apply_refuses_after_a_failed_ensure(session, service, knowledge_root, monkeypatch):
    created = create_context(session, title="SAP", type="application")
    monkeypatch.setattr(service._store, "ensure_context_knowledge", raise_filesystem, raising=True)
    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)
    monkeypatch.undo()

    with pytest.raises(ContextKnowledgeNotInitializedError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Zu frueh.")
        )

    assert tree(knowledge_root) == set()


def test_apply_accepts_a_manually_created_complete_structure(
    session, service, knowledge_store, knowledge_root
):
    """Die Sperre gilt der fehlenden Initialisierung, nicht dem Weg dorthin."""
    created = create_context(session, title="SAP", type="application", knowledge_path=MANUAL_PATH)
    knowledge_store.ensure_context_knowledge(MANUAL_PATH, title="SAP")

    result = service.apply(
        ContextKnowledgeUpdate(context_id=created.id, action="append_note", text="Jetzt passt es.")
    )

    assert result.changed is True
    assert "Jetzt passt es." in (
        knowledge_root / "Bewerbungen" / "SAP 2026" / "notes.md"
    ).read_text(encoding="utf-8")
