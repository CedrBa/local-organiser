"""Apply contracts for context_knowledge."""

from __future__ import annotations

import pytest
from organiser.db.models import Context
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    KNOWLEDGE_WRITE_COMPLETED_EVENT,
    KNOWLEDGE_WRITE_INTENT_EVENT,
    KNOWLEDGE_WRITE_NOOP_EVENT,
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
    shared_knowledge_store,
)
from organiser.services.domain import (
    NotFoundError,
    add_context_source,
    create_context,
    get_context,
    list_contexts,
    list_events_for_entity,
)
from organiser.services.knowledge import KnowledgeStore
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


# -- transaction boundary -----------------------------------------------------


def test_service_refuses_to_run_inside_a_deferred_domain_commit(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    session.info["defer_domain_commit"] = True
    try:
        with pytest.raises(ContextKnowledgeError):
            service.ensure_context_knowledge(created.id)
    finally:
        session.info.pop("defer_domain_commit")

    assert tree(knowledge_root) == set()
    assert get_context(session, created.id).knowledge_path is None


def test_service_refuses_to_commit_foreign_pending_changes(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    foreign = Context(title="Fremder Entwurf", type="topic")
    session.add(foreign)

    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge(created.id)

    session.rollback()
    assert tree(knowledge_root) == set()
    assert [item.title for item in list_contexts(session)] == ["SAP"]


# -- controlled apply ---------------------------------------------------------


def test_apply_on_an_uninitialized_context_is_refused_without_side_effects(
    session, service, knowledge_root
):
    created = create_context(session, title="SAP", type="application")

    with pytest.raises(ContextKnowledgeNotInitializedError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="update_overview", text="Zu frueh."
            )
        )

    # Kein versteckter Init-Lifecycle ueber Store-Nebenwirkungen.
    assert tree(knowledge_root) == set()
    assert get_context(session, created.id).knowledge_path is None
    assert event_types(session, created.id) == ["CONTEXT_CREATED"]


def test_apply_on_an_unknown_context_has_no_filesystem_side_effects(
    session, service, knowledge_root
):
    with pytest.raises(NotFoundError):
        service.apply(
            ContextKnowledgeUpdate(context_id="gibt-es-nicht", action="update_overview", text="x")
        )
    assert tree(knowledge_root) == set()


def test_apply_uses_the_bound_path_and_audits_operation_file_and_hash(
    session, service, knowledge_root
):
    created = create_context(session, title="SAP", type="application")
    path = service.ensure_context_knowledge(created.id).knowledge_path

    result = service.apply(
        ContextKnowledgeUpdate(
            context_id=created.id, action="update_progress", status="in_progress", percent=40
        )
    )

    assert result.knowledge_path == path
    assert result.file == "progress.md"
    assert result.changed is True
    assert "40 %" in (knowledge_root / path / "progress.md").read_text(encoding="utf-8")
    completion = list_events_for_entity(session, entity_type="context", entity_id=created.id)[-1]
    assert completion.event_type == KNOWLEDGE_WRITE_COMPLETED_EVENT
    assert completion.entity_type == "context"
    assert completion.payload["operation"] == "update_progress"
    assert completion.payload["file"] == "progress.md"
    assert completion.payload["changed"] is True
    assert completion.payload["content_hash"] == result.content_hash
    assert str(knowledge_root) not in str(completion.payload)


def test_repeating_the_same_apply_is_a_noop(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    path = service.ensure_context_knowledge(created.id).knowledge_path
    update = ContextKnowledgeUpdate(
        context_id=created.id, action="update_overview", text="Stabiler Ueberblick."
    )

    first = service.apply(update)
    before = (knowledge_root / path / "overview.md").read_text(encoding="utf-8")
    second = service.apply(update)
    after = (knowledge_root / path / "overview.md").read_text(encoding="utf-8")

    assert first.changed is True
    assert second.changed is False
    assert before == after
    assert event_types(session, created.id)[-1] == KNOWLEDGE_WRITE_NOOP_EVENT
    assert event_types(session, created.id).count(KNOWLEDGE_WRITE_INTENT_EVENT) == 2


def test_apply_never_touches_context_source_files(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    outside = knowledge_root / "quellen"
    outside.mkdir()
    (outside / "fragebogen.pdf").write_text("extern", encoding="utf-8")
    add_context_source(
        session, context_id=created.id, source_type="document", path="quellen/fragebogen.pdf"
    )
    service.ensure_context_knowledge(created.id)

    before = sorted(str(item) for item in knowledge_root.rglob("*"))
    service.apply(
        ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="Nur intern.")
    )

    assert sorted(str(item) for item in knowledge_root.rglob("*")) == before
    assert (outside / "fragebogen.pdf").read_text(encoding="utf-8") == "extern"


# -- write coordination -------------------------------------------------------


def test_shared_store_instance_is_reused_per_root(tmp_path, monkeypatch):
    monkeypatch.setenv("ORGANISER_KNOWLEDGE_ROOT", str(tmp_path / "wissen"))
    first = shared_knowledge_store()
    second = shared_knowledge_store()
    assert first is second
    assert isinstance(first, KnowledgeStore)
    other_root = KnowledgeStore(tmp_path / "anderes-wissen")
    assert other_root is not first
