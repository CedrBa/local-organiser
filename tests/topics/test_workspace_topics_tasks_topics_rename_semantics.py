"""Rename semantics contracts for topics."""

from __future__ import annotations

from organiser.services.domain import (
    list_events_for_entity,
)
from organiser.services.topics import (
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


def test_empty_patch_does_not_silently_recanonicalise_a_stored_title_key(session, context_a):
    topic = write_non_canonical_topic(
        session, context_a.id, title="Bilanzierung", title_key="bilanz ierung"
    )
    session.commit()
    before = stored_topic_row(session, topic.id)
    before_events = len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id))

    assert update_topic(session, topic_id=topic.id) is topic

    assert stored_topic_row(session, topic.id) == before
    assert (
        len(list_events_for_entity(session, entity_type="topic", entity_id=topic.id))
        == before_events
    )


def test_a_patch_naming_the_stored_title_is_also_a_true_noop(session, context_a):
    topic = write_non_canonical_topic(
        session, context_a.id, title="Bilanzierung", title_key="bilanz ierung"
    )
    session.commit()
    before = stored_topic_row(session, topic.id)

    # Only the *display* title is normalised (trim/collapse); case belongs to the
    # title, the *key* is what folds. So this patch names the same title.
    assert update_topic(session, topic_id=topic.id, title="  Bilanzierung  ") is topic

    assert stored_topic_row(session, topic.id) == before
    assert not list_events_for_entity(session, entity_type="topic", entity_id=topic.id)


def test_a_case_only_patch_is_a_real_rename_that_keeps_the_folded_key(session, context_a):
    topic = write_non_canonical_topic(
        session, context_a.id, title="Bilanzierung", title_key="bilanzierung"
    )
    session.commit()
    before = stored_topic_row(session, topic.id)

    update_topic(session, topic_id=topic.id, title="BILANZIERUNG")

    after = stored_topic_row(session, topic.id)
    assert after["title"] == "BILANZIERUNG"
    assert after["title_key"] == before["title_key"]
    assert after["updated_at"] != before["updated_at"]


def test_a_real_rename_does_canonicalise_the_stored_title_key(session, context_a):
    topic = write_non_canonical_topic(
        session, context_a.id, title="Bilanzierung", title_key="bilanz ierung"
    )

    renamed = update_topic(session, topic_id=topic.id, title="Abschreibung")

    assert renamed.title == "Abschreibung" and renamed.title_key == "abschreibung"
    event = list_events_for_entity(session, entity_type="topic", entity_id=topic.id)[-1]
    assert event.payload["changes"]["title_key"] == {"old": "bilanz ierung", "new": "abschreibung"}


def test_empty_patch_cannot_manufacture_a_conflict_between_two_manual_rows(session, context_a):
    first = write_non_canonical_topic(
        session, context_a.id, title="Bilanzierung", title_key="  BILANZIERUNG  "
    )
    write_non_canonical_topic(session, context_a.id, title="Bilanzierung", title_key="bilanzierung")

    assert update_topic(session, topic_id=first.id) is first

    assert stored_topic_row(session, first.id)["title_key"] == "  BILANZIERUNG  "
