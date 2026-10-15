"""Binding freshness contracts for context_knowledge."""

from __future__ import annotations

import pytest
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    KNOWLEDGE_PATH_BOUND_EVENT,
)
from organiser.services.domain import (
    ConflictError,
    create_context,
    list_events_for_entity,
    update_context,
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


# -- 2. Frische, unveraenderliche Bindung -------------------------------------


def test_ensure_does_not_overwrite_a_binding_committed_by_another_session(
    session, service, knowledge_store, knowledge_root, other_session
):
    created = create_context(session, title="SAP", type="application")
    stale, _record, stale_writer = stale_session(session, created.id, knowledge_store)

    # Session B bindet bewusst manuell und committet.
    update_context(session, context_id=created.id, knowledge_path=MANUAL_PATH)

    result = stale_writer.ensure_context_knowledge(created.id)

    assert result.knowledge_path == MANUAL_PATH
    assert result.bound_now is False
    assert stored_path(other_session, created.id) == MANUAL_PATH
    assert (knowledge_root / "Bewerbungen" / "SAP 2026" / "overview.md").is_file()
    assert not (knowledge_root / "contexts" / created.id).exists()
    assert KNOWLEDGE_PATH_BOUND_EVENT not in event_types(session, created.id)
    stale.close()


def test_apply_uses_the_binding_of_the_database_not_the_stale_one(
    session, knowledge_store, knowledge_root
):
    created = create_context(session, title="SAP", type="application")
    stale, _record, stale_writer = stale_session(session, created.id, knowledge_store)
    update_context(session, context_id=created.id, knowledge_path=MANUAL_PATH)
    stale_writer.ensure_context_knowledge(created.id)

    result = stale_writer.apply(
        ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="Aus der DB")
    )

    assert result.knowledge_path == MANUAL_PATH
    assert result.file == "overview.md"
    assert "Aus der DB" in (knowledge_root / "Bewerbungen" / "SAP 2026" / "overview.md").read_text(
        encoding="utf-8"
    )
    stale.close()


def test_domain_patch_cannot_overwrite_a_binding_from_another_session(
    session, knowledge_store, other_session
):
    """Auch der Domain-PATCH haelt die Invariante ein: nur NULL wird ersetzt."""
    created = create_context(session, title="SAP", type="application")
    stale, _record, _writer = stale_session(session, created.id, knowledge_store)
    update_context(session, context_id=created.id, knowledge_path=MANUAL_PATH)

    with pytest.raises(ConflictError):
        update_context(stale, context_id=created.id, knowledge_path="Anders/Woanders")

    assert stored_path(other_session, created.id) == MANUAL_PATH
    stale.close()


def test_domain_patch_with_the_stored_value_stays_a_noop(session, knowledge_store, other_session):
    created = create_context(session, title="SAP", type="application")
    stale, _record, _writer = stale_session(session, created.id, knowledge_store)
    update_context(session, context_id=created.id, knowledge_path=MANUAL_PATH)
    before = len(list_events_for_entity(session, entity_type="context", entity_id=created.id))

    update_context(stale, context_id=created.id, knowledge_path=MANUAL_PATH)

    assert stored_path(other_session, created.id) == MANUAL_PATH
    assert (
        len(list_events_for_entity(session, entity_type="context", entity_id=created.id)) == before
    )
    # Auch das zurueckgegebene Objekt darf keinen veralteten Stand melden.
    assert update_context(stale, context_id=created.id).knowledge_path == MANUAL_PATH
    stale.close()
