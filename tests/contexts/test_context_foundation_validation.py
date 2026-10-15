"""Validation contracts for contexts."""

from __future__ import annotations

import pytest
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    create_context,
    get_context,
    list_context_sources,
    list_contexts,
    list_events_for_entity,
    update_context,
)
from support.context_foundation import (
    session as session,
    without_timestamps as without_timestamps,
    alembic_config as alembic_config,
    connect as connect,
    table_names as table_names,
    create_legacy_data as create_legacy_data,
    read_legacy_data as read_legacy_data,
    client as client,
)


def test_context_is_created_without_any_source(session):
    created = create_context(session, title="Finanzierung und Rechnungswesen", type="topic")

    assert created.status == "active"
    assert created.parent_context_id is None
    assert created.knowledge_path is None
    assert list_context_sources(session, context_id=created.id) == []
    assert [context.id for context in list_contexts(session)] == [created.id]
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == ["CONTEXT_CREATED"]


def test_context_title_and_type_are_required(session):
    with pytest.raises(DomainValidationError):
        create_context(session, title="   ", type="topic")
    with pytest.raises(DomainValidationError):
        create_context(session, title="Bewerbung", type="  ")
    assert list_contexts(session) == []


@pytest.mark.parametrize("field", ["title", "type"])
def test_domain_update_rejects_null_for_required_values(session, field):
    """Direkte Domain-Aufrufe liefern DomainValidationError, kein AttributeError."""
    created = create_context(session, title="Bewerbungen", type="topic")

    with pytest.raises(DomainValidationError):
        update_context(session, context_id=created.id, **{field: None})

    stored = get_context(session, created.id)
    assert stored.title == "Bewerbungen"
    assert stored.type == "topic"
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == ["CONTEXT_CREATED"]


@pytest.mark.parametrize("field", ["title", "type"])
def test_domain_create_rejects_null_for_required_values(session, field):
    """Direkte Domain-Aufrufe liefern DomainValidationError, kein AttributeError."""
    values = {"title": "Bewerbungen", "type": "topic"}
    values[field] = None

    with pytest.raises(DomainValidationError):
        create_context(session, **values)

    assert list_contexts(session) == []


@pytest.mark.parametrize("field", ["title", "type"])
def test_domain_null_and_wrong_types_raise_domain_validation_error(session, field):
    """Auch falsche Python-Typen duerfen nicht als AttributeError durchschlagen."""
    create_values = {"title": "Neu", "type": "topic"}
    create_values[field] = 42
    with pytest.raises(DomainValidationError):
        create_context(session, **create_values)

    created = create_context(session, title="Bewerbungen", type="topic")
    with pytest.raises(DomainValidationError):
        update_context(session, context_id=created.id, **{field: 42})

    assert get_context(session, created.id).title == "Bewerbungen"


def test_knowledge_path_must_be_relative_and_canonical(session):
    accepted = [
        "bewerbungen/sap",
        "a",
        "a/b/c/d",
        "Bewerbungen/SAP 2026",
        "a-b_c.d",
        "a/" * 255 + "x",
    ]
    for path in accepted:
        created = create_context(session, title="SAP", type="application", knowledge_path=path)
        assert created.knowledge_path == path

    rejected = [
        "",
        "   ",
        "/absolut/pfad",
        "//server/share",
        "C:/pfad",
        "c:\\pfad",
        "\\\\server\\share",
        "bewerbungen\\sap",
        "a//b",
        "a/./b",
        "./a",
        ".",
        "..",
        "a/../b",
        "../a",
        "a/b/",
        "a\x00b",
        "a\nb",
        "a\tb",
        "a\x7fb",
        " lead/pfad",
        "pfad/lead ",
        "a/ lead",
        "a/ /b",
        "a" * 513,
        "a/" * 256 + "x",
    ]
    for path in rejected:
        with pytest.raises(DomainValidationError):
            create_context(session, title="SAP", type="application", knowledge_path=path)


def test_knowledge_path_rejection_preserves_nothing_and_writes_no_event(session):
    """Abgelehnte Pfade aendern weder Daten noch Events."""
    created = create_context(
        session, title="SAP", type="application", knowledge_path="bewerbungen/sap"
    )

    for path in ("a//b", "a/./b", "/absolut", "a\x00b", "a" * 513):
        with pytest.raises(DomainValidationError):
            update_context(session, context_id=created.id, knowledge_path=path)

    assert get_context(session, created.id).knowledge_path == "bewerbungen/sap"
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == ["CONTEXT_CREATED"]


def test_knowledge_path_none_keeps_meaning_not_yet_bound(session):
    created = create_context(session, title="SAP", type="application")
    assert created.knowledge_path is None

    bound = update_context(session, context_id=created.id, knowledge_path="bewerbungen/sap")
    assert bound.knowledge_path == "bewerbungen/sap"

    # Gebunden heisst dauerhaft: Ruecksetzen ueber das normale Context-PATCH ist
    # nicht mehr moeglich, sonst loeste sich der Verweis vom geschriebenen Inhalt.
    with pytest.raises(ConflictError):
        update_context(session, context_id=created.id, knowledge_path=None)
    assert (
        update_context(session, context_id=created.id, title="SAP 2026").knowledge_path
        == "bewerbungen/sap"
    )


def test_setting_the_same_knowledge_path_again_is_a_no_op(session):
    """Der identische Wert bleibt erlaubt und erzeugt kein zweites Ereignis."""
    created = create_context(
        session, title="SAP", type="application", knowledge_path="bewerbungen/sap"
    )

    unchanged = update_context(session, context_id=created.id, knowledge_path="bewerbungen/sap")

    assert unchanged.knowledge_path == "bewerbungen/sap"
    assert [
        event.event_type
        for event in list_events_for_entity(session, entity_type="context", entity_id=created.id)
    ] == ["CONTEXT_CREATED"]


def test_two_contexts_cannot_bind_the_same_knowledge_path(session):
    """Nicht-NULL knowledge_path ist eindeutig; die Domain prueft vor der DB."""
    create_context(session, title="SAP", type="application", knowledge_path="bewerbungen/sap")
    other = create_context(session, title="Finanzierung", type="topic")

    with pytest.raises(ConflictError):
        create_context(session, title="Dritter", type="topic", knowledge_path="bewerbungen/sap")
    with pytest.raises(ConflictError):
        update_context(session, context_id=other.id, knowledge_path="bewerbungen/sap")

    assert get_context(session, other.id).knowledge_path is None


def test_knowledge_path_rejects_reserved_store_segments(session):
    """Ein Context-Pfad darf nicht auf die Struktur des Stores zeigen."""
    for path in (
        "topics",
        "bewerbungen/topics",
        "notes.md",
        "bewerbungen/Overview.md",
        "contexts/x/progress.md",
    ):
        with pytest.raises(DomainValidationError):
            create_context(session, title="SAP", type="application", knowledge_path=path)
    assert list_contexts(session) == []


def test_knowledge_path_validation_uses_no_filesystem_access(session, monkeypatch):
    """Die Pfadpruefung liest keine Settings und beruehrt das Dateisystem nicht."""
    import pathlib as pathlib_module

    def explode(*args, **kwargs):
        raise AssertionError("Dateisystemzugriff in der Pfadpruefung")

    monkeypatch.setattr(pathlib_module.Path, "exists", explode)
    monkeypatch.setattr(pathlib_module.Path, "is_dir", explode)
    monkeypatch.setattr(pathlib_module.Path, "resolve", explode)
    monkeypatch.setattr(pathlib_module, "Path", explode)

    created = create_context(
        session, title="SAP", type="application", knowledge_path="bewerbungen/sap"
    )
    assert created.knowledge_path == "bewerbungen/sap"
    with pytest.raises(DomainValidationError):
        create_context(session, title="SAP", type="application", knowledge_path="/etc/passwd")
