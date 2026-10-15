"""Initialization contracts for context_knowledge."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from organiser.domain.knowledge import ContextKnowledgeUpdate, KnowledgeUpdate
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    default_knowledge_path,
    validate_context_path,
    validate_knowledge_path,
)
from organiser.services.context_knowledge import (
    KNOWLEDGE_INIT_COMPLETED_EVENT,
    KNOWLEDGE_INIT_INTENT_EVENT,
    KNOWLEDGE_PATH_BOUND_EVENT,
    ContextKnowledgeError,
)
from organiser.services.domain import (
    ConflictError,
    EventContext,
    NotFoundError,
    create_context,
    get_context,
    list_context_sources,
    list_events_for_entity,
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


@pytest.mark.parametrize(
    "segment", ["topics", "overview.md", "progress.md", "notes.md", "Notes.MD", "TOPICS"]
)
def test_shared_validator_rejects_reserved_store_segments(segment):
    with pytest.raises(KnowledgePathError):
        validate_context_path(f"contexts/{segment}")
    # Der Store darf seine eigenen Dateinamen als Datei adressieren.
    assert validate_knowledge_path(f"contexts/x/{segment}", allow_reserved_segments=True)


def test_shared_validator_has_no_filesystem_or_settings_dependency(monkeypatch):
    """Der Validator liest weder Dateisystem noch Settings und normalisiert nichts."""
    import pathlib as pathlib_module

    def explode(*args, **kwargs):
        raise AssertionError("Dateisystemzugriff in der Pfadpruefung")

    monkeypatch.setenv("ORGANISER_KNOWLEDGE_ROOT", "/nirgends")
    monkeypatch.setattr(pathlib_module.Path, "exists", explode)
    monkeypatch.setattr(pathlib_module.Path, "is_dir", explode)
    monkeypatch.setattr(pathlib_module.Path, "resolve", explode)

    assert validate_context_path("Bewerbungen/SAP 2026") == "Bewerbungen/SAP 2026"
    assert default_knowledge_path("abc-123") == "contexts/abc-123"


def test_context_path_normalization_is_never_silent():
    """Keine stillschweigende Reparatur: der Wert kommt unveraendert zurueck."""
    for rejected in ("a//b", "a/./b", "a/b/", " a/b", "a/b ", "a\\b"):
        with pytest.raises(KnowledgePathError):
            validate_context_path(rejected)


# -- context-bound DTO --------------------------------------------------------


def test_context_update_dto_has_no_context_path():
    """Die DTO kann keinen Pfad ausdruecken; context_id ist die einzige Adresse."""
    for payload in (
        {"context_id": "c1", "action": "update_overview", "text": "x", "context_path": "a/b"},
        {"context_id": "c1", "action": "update_overview", "text": "x", "path": "a/b"},
        {"context_id": "c1", "action": "update_overview", "text": "x", "absolute_path": "/a/b"},
        {"context_id": "c1", "action": "update_overview", "text": "x", "filename": "overview.md"},
    ):
        with pytest.raises(ValidationError):
            ContextKnowledgeUpdate(**payload)

    assert "context_path" not in ContextKnowledgeUpdate.model_fields
    assert ContextKnowledgeUpdate.model_config["extra"] == "forbid"


def test_context_update_dto_keeps_action_consistency_rules():
    with pytest.raises(ValidationError):
        ContextKnowledgeUpdate(context_id="c1", action="update_progress")
    with pytest.raises(ValidationError):
        ContextKnowledgeUpdate(context_id="c1", action="append_note", text="   ")
    with pytest.raises(ValidationError):
        ContextKnowledgeUpdate(context_id="c1", action="update_topic", text="x")
    with pytest.raises(ValidationError):
        ContextKnowledgeUpdate(context_id="c1", action="update_overview", text="x", percent=10)

    # Nur an die ID adressiert; der Pfad entsteht erst im Service.
    update = ContextKnowledgeUpdate(context_id="c1", action="update_progress", percent=50)
    assert update.to_knowledge_update("contexts/c1").context_path == "contexts/c1"


def test_knowledge_update_still_requires_a_path():
    assert "context_path" in KnowledgeUpdate.model_fields


# -- lifecycle: ensure --------------------------------------------------------


def test_context_without_sources_and_without_knowledge_stays_possible(session, knowledge_root):
    """Ein Context darf vollstaendig ohne Sources und ohne Knowledge existieren."""
    created = create_context(session, title="Finanzierung", type="topic")
    assert list_context_sources(session, context_id=created.id) == []
    assert get_context(session, created.id).knowledge_path is None
    assert tree(knowledge_root) == set()
    assert event_types(session, created.id) == ["CONTEXT_CREATED"]


def test_unknown_context_has_no_filesystem_side_effect(service, knowledge_root):
    with pytest.raises(NotFoundError):
        service.ensure_context_knowledge("gibt-es-nicht")
    assert tree(knowledge_root) == set()


def test_unknown_context_id_is_rejected_before_any_lookup(service, knowledge_root):
    with pytest.raises(ContextKnowledgeError):
        service.ensure_context_knowledge("   ")
    assert tree(knowledge_root) == set()


def test_ensure_binds_default_path_and_creates_structure(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")

    result = service.ensure_context_knowledge(created.id)

    assert result.knowledge_path == f"contexts/{created.id}"
    assert result.bound_now is True
    assert result.changed is True
    assert set(result.created_files) == {"overview.md", "progress.md", "notes.md"}
    assert get_context(session, created.id).knowledge_path == f"contexts/{created.id}"
    assert event_types(session, created.id) == [
        "CONTEXT_CREATED",
        KNOWLEDGE_PATH_BOUND_EVENT,
        KNOWLEDGE_INIT_INTENT_EVENT,
        KNOWLEDGE_INIT_COMPLETED_EVENT,
    ]


def test_ensure_keeps_an_explicitly_set_safe_path(session, service, knowledge_root):
    created = create_context(
        session, title="SAP", type="application", knowledge_path="Bewerbungen/SAP 2026"
    )

    result = service.ensure_context_knowledge(created.id)

    assert result.knowledge_path == "Bewerbungen/SAP 2026"
    assert result.bound_now is False
    assert (knowledge_root / "Bewerbungen" / "SAP 2026" / "overview.md").is_file()
    assert tree(knowledge_root).isdisjoint({f"contexts/{created.id}"})


def test_ensure_preserves_event_context_metadata(session, service):
    created = create_context(session, title="SAP", type="application")
    marker = EventContext(source_type="manual", source_id="note-7", correlation_id="corr-1")

    service.ensure_context_knowledge(created.id, context=marker)

    events = list_events_for_entity(session, entity_type="context", entity_id=created.id)
    knowledge_events = [
        event for event in events if event.event_type.startswith("CONTEXT_KNOWLEDGE")
    ]
    assert [event.event_type for event in knowledge_events] == [
        KNOWLEDGE_PATH_BOUND_EVENT,
        KNOWLEDGE_INIT_INTENT_EVENT,
        KNOWLEDGE_INIT_COMPLETED_EVENT,
    ]
    assert {event.source_type for event in knowledge_events} == {"manual"}
    assert {event.source_id for event in knowledge_events} == {"note-7"}
    assert {event.correlation_id for event in knowledge_events} == {"corr-1"}
    assert {event.entity_type for event in knowledge_events} == {"context"}
    assert {event.entity_id for event in knowledge_events} == {created.id}


def test_default_path_survives_rename_and_reparenting(session, service):
    parent = create_context(session, title="Bewerbungen", type="topic")
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    expected = f"contexts/{created.id}"

    renamed = update_context(session, context_id=created.id, title="SAP Bewerbung 2026")
    reparented = update_context(session, context_id=created.id, parent_context_id=parent.id)

    assert get_context(session, created.id).knowledge_path == expected
    assert renamed.knowledge_path == expected
    assert reparented.knowledge_path == expected
    assert service.ensure_context_knowledge(created.id).knowledge_path == expected
    assert get_context(session, parent.id).knowledge_path is None


def test_two_contexts_can_never_share_one_knowledge_storage(session, service, knowledge_root):
    first = create_context(session, title="SAP", type="application")
    second = create_context(session, title="Finanzierung", type="topic")
    first_result = service.ensure_context_knowledge(first.id)

    second_result = service.ensure_context_knowledge(second.id)

    assert first_result.knowledge_path != second_result.knowledge_path
    assert (knowledge_root / first_result.knowledge_path).is_dir()
    assert (knowledge_root / second_result.knowledge_path).is_dir()
    with pytest.raises(ConflictError):
        create_context(
            session, title="Dritter", type="topic", knowledge_path=first_result.knowledge_path
        )
