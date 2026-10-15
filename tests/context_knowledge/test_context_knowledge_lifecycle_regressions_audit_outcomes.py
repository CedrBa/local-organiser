"""Audit outcomes contracts for context_knowledge."""

from __future__ import annotations

import dataclasses
import os
import pytest
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.services.context_knowledge import (
    KNOWLEDGE_INIT_COMPLETED_EVENT,
    KNOWLEDGE_INIT_INTENT_EVENT,
    KNOWLEDGE_INIT_NOOP_EVENT,
    KNOWLEDGE_PATH_BOUND_EVENT,
    KNOWLEDGE_WRITE_COMPLETED_EVENT,
    KNOWLEDGE_WRITE_FAILED_EVENT,
    KNOWLEDGE_WRITE_INTENT_EVENT,
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeError,
)
from organiser.services.domain import (
    EventContext,
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


# -- 4. Audit, Verifikation und Fehlerhelligkeit ------------------------------


def test_every_operation_carries_its_own_operation_id(session, service):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    service.apply(ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="A"))
    service.apply(
        ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="B"),
        context=EventContext(correlation_id="corr-1"),
    )

    events = knowledge_events(session, created.id)
    ids = [event.payload["operation_id"] for event in events]
    assert all(isinstance(value, str) and value for value in ids)

    intents = [event for event in events if event.event_type.endswith("INTENT")]
    completions = [event for event in events if event.event_type.endswith(("_COMPLETED", "_NOOP"))]
    assert len(intents) == len(completions) == 3
    intent_ids = {event.payload["operation_id"] for event in intents}
    # Drei Vorgangskennungen fuer drei Aufrufe -- jede eindeutig, jede im Abschluss
    # wieder aufgefunden, auch ohne EventContext.
    assert len(intent_ids) == 3
    assert {event.payload["operation_id"] for event in completions} == intent_ids
    assert set(ids) == intent_ids
    assert completions[0].correlation_id is None
    assert completions[-1].correlation_id == "corr-1"


def test_repeated_operations_never_share_an_operation_id(session, service):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    update = ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="Stabil.")

    service.apply(update)
    service.apply(update)

    events = [
        event for event in knowledge_events(session, created.id) if "WRITE" in event.event_type
    ]
    intents = {
        event.payload["operation_id"]
        for event in events
        if event.event_type == KNOWLEDGE_WRITE_INTENT_EVENT
    }
    endings = [
        event.payload["operation_id"]
        for event in events
        if event.event_type.endswith(("_COMPLETED", "_NOOP"))
    ]
    # Erster Aufruf aendert, der zweite ist ein echter No-op -- zwei verschiedene
    # Vorgangskennungen, kein Abschluss kann den fremden Intent "erledigen".
    assert len(intents) == 2
    assert len(endings) == 2
    assert len(set(endings)) == 2
    assert set(endings) == intents


def test_binding_event_belongs_to_the_ensure_operation(session, service):
    created = create_context(session, title="SAP", type="application")

    service.ensure_context_knowledge(created.id)

    events = knowledge_events(session, created.id)
    assert events[0].event_type == KNOWLEDGE_PATH_BOUND_EVENT
    assert events[1].event_type == KNOWLEDGE_INIT_INTENT_EVENT
    assert events[0].payload["operation_id"] == events[1].payload["operation_id"]


def test_topic_intent_names_the_real_slug_file(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)

    result = service.apply(
        ContextKnowledgeUpdate(
            context_id=created.id, action="create_topic", topic="Fourier Transformation"
        )
    )

    assert result.file == "topics/Fourier-Transformation.md"
    assert (
        knowledge_root / f"contexts/{created.id}" / "topics" / "Fourier-Transformation.md"
    ).is_file()
    intent = [
        event
        for event in knowledge_events(session, created.id)
        if event.event_type == KNOWLEDGE_WRITE_INTENT_EVENT
    ][-1]
    # Der Intent ist bei einem fehlenden Abschluss das einzige Recovery-Indiz.
    assert intent.payload["file"] == "topics/Fourier-Transformation.md"
    assert intent.payload["operation_id"] == result.operation_id


def test_ensure_that_only_repairs_the_topics_directory_is_a_completion(
    session, service, knowledge_root
):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    (knowledge_root / f"contexts/{created.id}" / "topics").rmdir()

    result = service.ensure_context_knowledge(created.id)

    assert result.changed is True
    assert result.directory_created is False
    assert (knowledge_root / f"contexts/{created.id}" / "topics").is_dir()
    assert event_types(session, created.id)[-2:] == [
        KNOWLEDGE_INIT_INTENT_EVENT,
        KNOWLEDGE_INIT_COMPLETED_EVENT,
    ]
    completion = knowledge_events(session, created.id)[-1]
    assert completion.payload["changed"] is True
    assert completion.payload["repaired"] == ["topics"]


def test_repeat_ensure_reports_a_real_noop(session, service):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)

    result = service.ensure_context_knowledge(created.id)

    assert result.changed is False
    assert event_types(session, created.id)[-1] == KNOWLEDGE_INIT_NOOP_EVENT
    assert knowledge_events(session, created.id)[-1].payload["operation_id"]


def test_completion_is_only_written_for_the_persisted_result(session, service, monkeypatch):
    """Der Abschluss behauptet nur, was tatsaechlich auf der Platte steht."""
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    real_apply = service._store.apply

    def lying_apply(update):
        write = real_apply(update)
        return dataclasses.replace(write, content=write.content + "\n# nie geschrieben\n")

    monkeypatch.setattr(service._store, "apply", lying_apply, raising=True)

    with pytest.raises(ContextKnowledgeAmbiguousError):
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="update_overview", text="Ehrlich")
        )

    assert KNOWLEDGE_WRITE_COMPLETED_EVENT not in event_types(session, created.id)
    failure = knowledge_events(session, created.id)[-1]
    assert failure.event_type == KNOWLEDGE_WRITE_FAILED_EVENT
    assert failure.payload["outcome"] == "unknown"


def test_failure_after_a_completed_replacement_is_not_reported_as_harmless(
    session, service, knowledge_root, monkeypatch
):
    """Ein Fehler *nach* dem atomaren Ersetzen darf nicht als folgenlos gelten."""
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    overview = knowledge_root / f"contexts/{created.id}" / "overview.md"
    real_replace = os.replace

    def replace_then_fail(source, target):
        real_replace(source, target)
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "replace", replace_then_fail, raising=True)

    with pytest.raises(ContextKnowledgeAmbiguousError):
        service.apply(
            ContextKnowledgeUpdate(
                context_id=created.id, action="update_overview", text="Halb geschrieben."
            )
        )

    monkeypatch.undo()
    assert "Halb geschrieben." in overview.read_text(encoding="utf-8")
    failure = knowledge_events(session, created.id)[-1]
    assert failure.event_type == KNOWLEDGE_WRITE_FAILED_EVENT
    assert failure.payload["outcome"] == "unknown"
    assert "nicht automatisch" in str(failure.payload.get("recovery", ""))


def test_input_error_before_any_write_is_reported_as_a_refusal(session, service, knowledge_root):
    created = create_context(session, title="SAP", type="application")
    service.ensure_context_knowledge(created.id)
    service.apply(
        ContextKnowledgeUpdate(context_id=created.id, action="create_topic", topic="Fourier")
    )

    with pytest.raises(ContextKnowledgeError) as failure:
        service.apply(
            ContextKnowledgeUpdate(context_id=created.id, action="create_topic", topic="Fourier")
        )

    assert not isinstance(failure.value, ContextKnowledgeAmbiguousError)
    payload = knowledge_events(session, created.id)[-1].payload
    assert payload["outcome"] == "not_written"
    assert (knowledge_root / f"contexts/{created.id}" / "topics" / "Fourier.md").is_file()


def test_ensure_failure_states_that_the_structure_may_be_partial(session, service, monkeypatch):
    created = create_context(session, title="SAP", type="application")
    context_id = created.id

    def half_initialise(context_path, *, title=None, overview=None):
        directory = service._store.context_directory(context_path)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "overview.md").write_text("# halb\n", encoding="utf-8")
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(service._store, "ensure_context_knowledge", half_initialise, raising=True)

    with pytest.raises(ContextKnowledgeError) as failure:
        service.ensure_context_knowledge(context_id)

    assert "teilinitialisiert" in str(failure.value)
    payload = knowledge_events(session, context_id)[-1].payload
    assert payload["outcome"] == "partial_structure_possible"

    # Der Context bleibt gueltig und reparierbar. Der Aufrufer beendet zuerst
    # seine eigene Transaktion; ein Attributzugriff auf ein abgelaufenes ORM-Objekt
    # wuerde sonst sofort wieder eine Transaktion oeffnen.
    monkeypatch.undo()
    session.rollback()
    repaired = service.ensure_context_knowledge(context_id)
    assert repaired.changed is True
    assert event_types(session, context_id)[-1] == KNOWLEDGE_INIT_COMPLETED_EVENT
