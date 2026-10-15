"""Boundary contracts for context_knowledge."""

from __future__ import annotations

import pytest
from organiser.services.context_knowledge import (
    KNOWLEDGE_INIT_COMPLETED_EVENT,
    KNOWLEDGE_INIT_INTENT_EVENT,
    KNOWLEDGE_INIT_NOOP_EVENT,
)
from organiser.services.domain import (
    ConflictError,
    create_context,
    get_context,
    update_context,
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


def test_bound_path_cannot_be_changed_or_cleared_by_normal_patch(session, service):
    created = create_context(session, title="SAP", type="application")
    bound = service.ensure_context_knowledge(created.id).knowledge_path

    with pytest.raises(ConflictError):
        update_context(session, context_id=created.id, knowledge_path=None)
    with pytest.raises(ConflictError):
        update_context(session, context_id=created.id, knowledge_path="anders/woanders")

    # Identischer Wert bleibt ein No-op.
    assert (
        update_context(session, context_id=created.id, knowledge_path=bound).knowledge_path == bound
    )
    assert get_context(session, created.id).knowledge_path == bound


def test_repeat_ensure_is_a_noop_and_keeps_existing_content(session, service, knowledge_store):
    created = create_context(session, title="SAP", type="application")
    first = service.ensure_context_knowledge(created.id)
    knowledge_store.update_overview(first.knowledge_path, "Von Hand gepflegter Ueberblick.")
    overview = knowledge_store.context_directory(first.knowledge_path) / "overview.md"
    before = overview.read_text(encoding="utf-8")

    second = service.ensure_context_knowledge(created.id)

    assert second.bound_now is False
    assert second.changed is False
    assert second.created_files == ()
    assert overview.read_text(encoding="utf-8") == before
    assert event_types(session, created.id)[-2:] == [
        KNOWLEDGE_INIT_INTENT_EVENT,
        KNOWLEDGE_INIT_NOOP_EVENT,
    ]


def test_partial_initialization_is_repaired_without_losing_content(
    session, service, knowledge_store
):
    created = create_context(session, title="SAP", type="application")
    first = service.ensure_context_knowledge(created.id)
    directory = knowledge_store.context_directory(first.knowledge_path)
    knowledge_store.update_overview(first.knowledge_path, "Von Hand gepflegter Ueberblick.")
    overview = (directory / "overview.md").read_text(encoding="utf-8")
    progress = (directory / "progress.md").read_text(encoding="utf-8")
    # Teilinitialisierung: eine Kerndatei und der Topic-Ordner fehlen.
    (directory / "notes.md").unlink()
    (directory / "topics").rmdir()

    repaired = service.ensure_context_knowledge(created.id)

    assert set(repaired.created_files) == {"notes.md"}
    assert repaired.directory_created is False
    assert (directory / "overview.md").read_text(encoding="utf-8") == overview
    assert (directory / "progress.md").read_text(encoding="utf-8") == progress
    assert (directory / "notes.md").is_file()
    assert (directory / "topics").is_dir()
    assert event_types(session, created.id)[-1] == KNOWLEDGE_INIT_COMPLETED_EVENT
