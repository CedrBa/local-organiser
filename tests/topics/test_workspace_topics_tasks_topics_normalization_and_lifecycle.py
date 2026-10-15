"""Normalization and lifecycle contracts for topics."""

from __future__ import annotations

import pytest
from organiser.db.models import Topic
from organiser.domain.topics import (
    TOPIC_STATUSES,
    TopicInputError,
    knowledge_key_for,
    normalize_title_key,
    validate_topic_status,
)
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
    list_events_for_entity,
)
from organiser.services.topics import lifecycle as topics_service
from organiser.services.topics import (
    create_topic,
    get_topic,
    set_topic_status,
    update_topic,
)
from support.workspace_topics_tasks_topics import (
    session as session,
    context_a as context_a,
    context_b as context_b,
    make_resource as make_resource,
    write_non_canonical_topic as write_non_canonical_topic,
    stored_topic_row as stored_topic_row,
    insert_rival_topic as insert_rival_topic,
    driver_transaction_is_open as driver_transaction_is_open,
    observer_factory as observer_factory,
    add_caller_work as add_caller_work,
    add_pending_colliding_topic as add_pending_colliding_topic,
    assert_caller_work_intact as assert_caller_work_intact,
    insert_duplicate_titles as insert_duplicate_titles,
    a_topic as a_topic,
    install_failing_audit_trigger as install_failing_audit_trigger,
)


# -- pure normalisation --------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Bilanzierung", "bilanzierung"),
        ("  Bilanzierung  ", "bilanzierung"),
        ("BILANZIERUNG", "bilanzierung"),
        ("Bilanzierung   Teil", "bilanzierung teil"),
        ("Bilanzierung\t\nTeil", "bilanzierung teil"),
        ("Straße", "strasse"),
        # NFKC: Vollbreite wird auf die normale Form gefaltet.
        ("Ｂｉｌａｎｚｉｅｒｕｎｇ", "bilanzierung"),
        ("𝐀", "a"),
    ],
)
def test_title_key_folds_equivalent_spellings(value, expected):
    assert normalize_title_key(value) == expected


@pytest.mark.parametrize("value", ["", "   ", "\t\n", None, "a" * 201])
def test_title_key_rejects_unusable_titles(value):
    with pytest.raises(TopicInputError):
        normalize_title_key(value)


def test_title_key_has_no_edge_whitespace_or_newline():
    assert normalize_title_key("  a   b  ") == "a b"


def test_knowledge_key_is_derived_from_the_id():
    assert knowledge_key_for("abc") == "topic-abc"
    assert knowledge_key_for("abc") == knowledge_key_for("abc")
    assert knowledge_key_for("abc") != knowledge_key_for("abd")


@pytest.mark.parametrize("value", ["", "   ", "a/b", "a\\b", "..", "a\x00b", None])
def test_knowledge_key_rejects_unsafe_ids(value):
    with pytest.raises(TopicInputError):
        knowledge_key_for(value)


def test_topic_status_vocabulary_is_exactly_four_values():
    assert TOPIC_STATUSES == ("not_started", "in_progress", "done", "blocked")
    for status in TOPIC_STATUSES:
        assert validate_topic_status(status) == status
    with pytest.raises(TopicInputError):
        validate_topic_status("archived")


# -- CRUD ----------------------------------------------------------------------


def test_create_topic_is_database_only(session, context_a, knowledge_root):
    before = sorted(item.name for item in knowledge_root.rglob("*"))
    topic = create_topic(session, context_id=context_a.id, title="  Bilanzierung  ")
    assert topic.title == "Bilanzierung"
    assert topic.title_key == "bilanzierung"
    assert topic.status == "not_started"
    assert topic.knowledge_key == f"topic-{topic.id}"
    assert sorted(item.name for item in knowledge_root.rglob("*")) == before
    assert not list(knowledge_root.rglob("*.md"))


def test_create_topic_in_unknown_context_raises(session):
    with pytest.raises(NotFoundError):
        create_topic(session, context_id="missing", title="X")


def test_duplicate_title_in_same_context_conflicts(session, context_a):
    create_topic(session, context_id=context_a.id, title="Bilanzierung")
    with pytest.raises(ConflictError):
        create_topic(session, context_id=context_a.id, title="  BILANZIERUNG  ")


def test_same_title_in_two_contexts_is_allowed(session, context_a, context_b):
    first = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    second = create_topic(session, context_id=context_b.id, title="Bilanzierung")
    assert first.id != second.id


def test_topic_title_is_rejected_when_empty(session, context_a):
    with pytest.raises(DomainValidationError):
        create_topic(session, context_id=context_a.id, title="   ")


def test_topic_status_must_be_one_of_four(session, context_a):
    with pytest.raises(DomainValidationError):
        create_topic(session, context_id=context_a.id, title="X", status="archived")


# -- rename --------------------------------------------------------------------


def test_rename_keeps_id_and_knowledge_key(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    renamed = update_topic(session, topic_id=topic.id, title="Bilanzierung Teil 2")
    assert renamed.id == topic.id
    assert renamed.knowledge_key == f"topic-{topic.id}"
    assert renamed.title == "Bilanzierung Teil 2"
    assert renamed.title_key == "bilanzierung teil 2"


def test_rename_to_an_existing_title_conflicts(session, context_a):
    first = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    create_topic(session, context_id=context_a.id, title="Anlagenbuchhaltung")
    with pytest.raises(ConflictError):
        update_topic(session, topic_id=first.id, title="Anlagenbuchhaltung")
    assert get_topic(session, first.id).title == "Bilanzierung"


def test_rename_to_an_own_fold_of_the_current_title_is_a_noop(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    update_topic(session, topic_id=topic.id, title="  BILANZIERUNG  ")
    assert get_topic(session, topic.id).title == "BILANZIERUNG"


def test_rename_writes_no_event_when_nothing_changes(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    before = len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id))
    update_topic(session, topic_id=topic.id)
    assert len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id)) == before


def test_rename_converts_a_title_key_refusal_into_a_domain_error(session, context_a, monkeypatch):
    """``normalize_title_key`` must never escape as a raw ``ValueError``.

    Measured: unreachable today -- no Unicode code point is accepted by
    ``normalize_title`` and folds to an empty key -- so the trigger has to be
    installed. What is pinned is the *conversion*, not the trigger: a domain rule
    added to ``normalize_title_key`` later would otherwise surface as an
    unhandled ``TopicInputError`` (a ``ValueError``) and answer 500 instead of the
    422 the input family promises.
    """
    topic = create_topic(session, context_id=context_a.id, title="Alpha")

    def refuse(_title):
        raise TopicInputError("title ergibt keinen verwendbaren title_key")

    monkeypatch.setattr(topics_service, "normalize_title_key", refuse)

    with pytest.raises(DomainValidationError) as failure:
        update_topic(session, topic_id=topic.id, title="Beta")
    assert isinstance(failure.value.__cause__, TopicInputError)
    # Refused before any mutation: the rename did not happen.
    assert get_topic(session, topic.id).title == "Alpha"


# -- status --------------------------------------------------------------------


def test_status_can_move_between_all_four_values(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    for status in ("in_progress", "blocked", "done", "not_started"):
        assert set_topic_status(session, topic_id=topic.id, status=status).status == status


def test_status_noop_writes_no_event(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    before = len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id))
    set_topic_status(session, topic_id=topic.id, status="not_started")
    assert len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id)) == before


def test_status_change_is_audited(session, context_a):
    topic = create_topic(
        session,
        context_id=context_a.id,
        title="Bilanzierung",
        context=EventContext(correlation_id="c1"),
    )
    set_topic_status(session, topic_id=topic.id, status="done")
    events = list_events_for_entity(session, entity_type="topic", entity_id=topic.id)
    change = [event for event in events if event.event_type == "TOPIC_STATUS_CHANGED"][-1]
    assert change.payload["changes"]["status"] == {"old": "not_started", "new": "done"}
    created = [event for event in events if event.event_type == "TOPIC_CREATED"][0]
    assert created.correlation_id == "c1"


def test_topic_has_no_percent_column():
    assert "percent" not in {column.name for column in Topic.__table__.columns}


def test_topic_has_no_archive_column():
    """Die bestehende Resource-Archivierung bleibt der Rueckzugsweg fuer Dokumente."""
    assert "archived" not in {column.name for column in Topic.__table__.columns}


def test_topic_has_no_parent_or_hierarchy():
    assert "parent_topic_id" not in {column.name for column in Topic.__table__.columns}
