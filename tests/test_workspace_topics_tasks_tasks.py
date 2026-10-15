"""Task <-> Context/Topic rules of the workspace slice.

Covers the contract of ``docs/architecture/0010-context-workspace-slice.md`` section
3 and ``docs/architecture/WORKSPACE_TOPICS_TASKS.md``:

* an omitted context is *not* the same request as an explicit ``null``,
* a task may exist without any context at all,
* parent and child that both carry a context must agree,
* a topic, a resource and a child may never end up in a different context than
  the task that references them -- and nothing is reassigned silently,
* no percent column and no automatic status coupling is introduced.
"""

import pytest

from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Resource, ResourceTopic, Task, Topic, new_id
from organiser.db.session import create_session_factory
from organiser.services.domain import (
    ConflictError,
    EventContext,
    NotFoundError,
    complete_task,
    create_context,
    create_task,
    get_task,
    list_events_for_entity,
    list_tasks,
    set_context_status,
    update_task_details,
)
from organiser.services.topics import (
    create_topic,
    link_resource_topic,
    link_task_resource,
    set_topic_status,
    unlink_resource_topic,
    unlink_task_resource,
)


@pytest.fixture
def session(tmp_path):
    settings = make_settings(tmp_path, "topics-tasks.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


@pytest.fixture
def context_a(session):
    return create_context(session, title="Finanzierung", type="course")


@pytest.fixture
def context_b(session):
    return create_context(session, title="Buchhaltung", type="course")


def make_resource(session, context, title="Dokument") -> Resource:
    resource = Resource(
        context_id=context.id,
        locator=f"/tmp/{new_id()}",
        relative_path=title,
        source_path_snapshot=f"/tmp/{new_id()}",
        title=title,
        resource_type="markdown",
    )
    session.add(resource)
    session.flush()
    return resource


# -- a task without any workspace reference ------------------------------------


def test_task_without_context_is_created_unchanged(session):
    task = create_task(session, title="Alte Aufgabe")
    assert task.context_id is None
    assert task.topic_id is None
    assert get_task(session, task.id).context_id is None


def test_legacy_task_without_context_is_not_hidden_by_context_filter(session, context_a):
    legacy = create_task(session, title="Altbestand")
    scoped = create_task(session, title="Im Context", context_id=context_a.id)
    assert [item.id for item in list_tasks(session, context_id=context_a.id)] == [scoped.id]
    assert legacy.id not in {item.id for item in list_tasks(session, context_id=context_a.id)}
    # Ohne Filter bleibt der Altbestand sichtbar.
    assert legacy.id in {item.id for item in list_tasks(session)}


# -- omitted vs explicit null on create ----------------------------------------


def test_omitted_context_inherits_parent_context_once(session, context_a):
    parent = create_task(session, title="Kursaufgabe", context_id=context_a.id)
    child = create_task(session, title="Teilaufgabe", parent_task_id=parent.id)
    assert child.context_id == context_a.id


def test_explicit_null_context_does_not_inherit(session, context_a):
    parent = create_task(session, title="Kursaufgabe", context_id=context_a.id)
    child = create_task(session, title="Teilaufgabe", parent_task_id=parent.id, context_id=None)
    assert child.context_id is None


def test_task_without_parent_has_no_context_by_default(session, context_a):
    task = create_task(session, title="Eigenständig")
    assert task.context_id is None
    assert context_a.id not in {task.context_id}


def test_child_and_parent_with_both_contexts_must_be_equal(session, context_a, context_b):
    parent = create_task(session, title="Parent", context_id=context_a.id)
    with pytest.raises(ConflictError):
        create_task(session, title="Child", parent_task_id=parent.id, context_id=context_b.id)


def test_child_may_repeat_the_parents_context(session, context_a):
    parent = create_task(session, title="Parent", context_id=context_a.id)
    child = create_task(session, title="Child", parent_task_id=parent.id, context_id=context_a.id)
    assert child.context_id == context_a.id


def test_inheritance_is_not_dynamic(session, context_a, context_b):
    """Ein spaeter hinzugefuegter Parent-Context uebernimmt nicht rueckwirkend."""
    parent = create_task(session, title="Parent")
    child = create_task(session, title="Child", parent_task_id=parent.id)
    update_task_details(session, task_id=parent.id, context_id=context_a.id)
    assert get_task(session, child.id).context_id is None


# -- topic references ----------------------------------------------------------


def test_topic_requires_the_same_context(session, context_a, context_b):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    with pytest.raises(ConflictError):
        create_task(session, title="Falsch", context_id=context_b.id, topic_id=topic.id)


def test_topic_without_context_is_refused(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    with pytest.raises(ConflictError):
        create_task(session, title="Ohne Context", topic_id=topic.id)


def test_task_with_topic_in_its_context_is_created(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    assert task.topic_id == topic.id


def test_unknown_context_and_topic_raise_not_found(session, context_a):
    with pytest.raises(NotFoundError):
        create_task(session, title="X", context_id="missing")
    with pytest.raises(NotFoundError):
        create_task(session, title="X", context_id=context_a.id, topic_id="missing")


def test_two_contexts_may_hold_topics_with_the_same_title(session, context_a, context_b):
    first = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    second = create_topic(session, context_id=context_b.id, title="Bilanzierung")
    assert first.id != second.id
    assert first.knowledge_key != second.knowledge_key


# -- context change and removal are refused, never cascaded --------------------


def test_context_change_with_topic_reference_conflicts(session, context_a, context_b):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, context_id=context_b.id)


def test_context_change_with_resource_reference_conflicts(session, context_a, context_b):
    resource = make_resource(session, context_a)
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, context_id=context_b.id)
    assert get_task(session, task.id).context_id == context_a.id


def test_removing_the_context_with_a_resource_reference_conflicts(session, context_a):
    resource = make_resource(session, context_a)
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, context_id=None)
    assert get_task(session, task.id).context_id == context_a.id


def test_removing_the_context_with_a_topic_reference_conflicts(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, context_id=None)


def test_explicit_unlink_before_context_change_succeeds(session, context_a, context_b):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    update_task_details(session, task_id=task.id, topic_id=None)
    update_task_details(session, task_id=task.id, context_id=context_b.id)
    reloaded = get_task(session, task.id)
    assert reloaded.context_id == context_b.id
    assert reloaded.topic_id is None


def test_context_change_with_divergent_children_conflicts(session, context_a, context_b):
    """Ein Kind mit eigenem Context blockiert den Contextwechsel des Parents.

    Der Ausgangszustand ist auf legendem Weg erreichbar: der Parent hat noch
    keinen Context, das Kind schon einen. Genau das ist der Fall, in dem ein
    Reparent- oder Contextwechsel sonst Children stillcrossing hinterlassen
    wuerde.
    """
    parent = create_task(session, title="Parent")
    create_task(session, title="Kind", parent_task_id=parent.id, context_id=context_b.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=parent.id, context_id=context_a.id)
    assert get_task(session, parent.id).context_id is None


def test_reparenting_into_another_context_conflicts(session, context_a, context_b):
    task = create_task(session, title="Kind", context_id=context_a.id)
    other_parent = create_task(session, title="Parent", context_id=context_b.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, parent_task_id=other_parent.id)
    assert get_task(session, task.id).parent_task_id is None


def test_reparenting_with_children_in_another_context_conflicts(session, context_a, context_b):
    child = create_task(session, title="Kind")
    create_task(session, title="Kind2", parent_task_id=child.id, context_id=context_b.id)
    stranger = create_task(session, title="Fremder Parent", context_id=context_a.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=child.id, parent_task_id=stranger.id)
    assert get_task(session, child.id).parent_task_id is None


def test_task_without_references_can_change_context(session, context_a, context_b):
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    update_task_details(session, task_id=task.id, context_id=context_b.id)
    assert get_task(session, task.id).context_id == context_b.id


def test_setting_a_foreign_topic_on_patch_conflicts(session, context_a, context_b):
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    topic = create_topic(session, context_id=context_b.id, title="Bilanzierung")
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, topic_id=topic.id)


def test_topic_set_without_context_on_patch_conflicts(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe")
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=task.id, topic_id=topic.id)


# -- no status coupling --------------------------------------------------------


def test_completing_a_task_does_not_touch_topic_or_context(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    complete_task(session, task_id=task.id)
    assert session.get(Topic, topic.id).status == "not_started"
    assert get_task(session, task.id).context_id == context_a.id


def test_done_topic_does_not_complete_a_task(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id, topic_id=topic.id)
    set_topic_status(session, topic_id=topic.id, status="done")
    assert get_task(session, task.id).status == "open"


def test_completing_a_context_does_not_complete_its_tasks_or_topics(session, context_a):
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    set_context_status(session, context_id=context_a.id, status="completed")
    assert get_task(session, task.id).status == "open"
    assert session.get(Topic, topic.id).status == "not_started"


def test_task_has_no_percent_column(session, context_a):
    assert "percent" not in {column.name for column in Task.__table__.columns}


# -- audit ---------------------------------------------------------------------


def test_context_change_is_audited(session, context_a, context_b):
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    update_task_details(
        session,
        task_id=task.id,
        context_id=context_b.id,
        context=EventContext(source_type="test", correlation_id="corr-1"),
    )
    events = list_events_for_entity(session, entity_type="task", entity_id=task.id)
    update = [event for event in events if event.event_type == "TASK_UPDATED"][-1]
    assert update.payload["changes"]["context_id"] == {"old": context_a.id, "new": context_b.id}
    assert update.correlation_id == "corr-1"


def test_noop_patch_writes_no_event(session, context_a):
    task = create_task(session, title="Aufgabe", context_id=context_a.id)
    before = len(list_events_for_entity(session, entity_type="task", entity_id=task.id))
    update_task_details(session, task_id=task.id, context_id=context_a.id)
    assert len(list_events_for_entity(session, entity_type="task", entity_id=task.id)) == before


# -- deferred commit -----------------------------------------------------------


def test_deferred_task_create_is_visible_only_after_the_caller_commits(
    session, tmp_path, context_a
):
    session.info["defer_domain_commit"] = True
    try:
        task = create_task(session, title="Aufgabe", context_id=context_a.id)
        # Im selben Flush sichtbar ...
        assert task.id is not None
        session.flush()
        # ... aber eine fremde Session sieht nichts, es gab keinen Commit.
        other = create_session_factory(make_settings(tmp_path, "topics-tasks.sqlite3"))()
        try:
            assert other.get(Task, task.id) is None
        finally:
            other.close()
    finally:
        session.info.pop("defer_domain_commit")
    session.rollback()
    assert session.get(Task, task.id) is None


def test_link_and_unlink_are_auditable_and_idempotent(session, context_a):
    resource = make_resource(session, context_a)
    topic = create_topic(session, context_id=context_a.id, title="Bilanzierung")
    link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    link_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    events = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="topic", entity_id=topic.id)
    ]
    assert events.count("RESOURCE_TOPIC_LINKED") == 1
    unlink_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    unlink_resource_topic(session, topic_id=topic.id, resource_id=resource.id)
    events = [
        event.event_type
        for event in list_events_for_entity(session, entity_type="topic", entity_id=topic.id)
    ]
    assert events.count("RESOURCE_TOPIC_UNLINKED") == 1
    assert (
        session.query(ResourceTopic).filter_by(resource_id=resource.id, topic_id=topic.id).count()
        == 0
    )


def test_title_update_keeps_a_valid_contextless_parent_hierarchy(session, context_a, context_b):
    grandparent = create_task(session, title="Root", context_id=context_a.id)
    parent = create_task(session, title="Parent", parent_task_id=grandparent.id, context_id=None)
    child = create_task(session, title="Child", parent_task_id=parent.id, context_id=context_b.id)
    update_task_details(session, task_id=parent.id, title="Neuer Titel")
    assert parent.title == "Neuer Titel"
    assert child.context_id == context_b.id


def test_context_removal_with_context_bound_children_requires_explicit_unlink(session, context_a):
    parent = create_task(session, title="Parent", context_id=context_a.id)
    child = create_task(session, title="Child", parent_task_id=parent.id)
    with pytest.raises(ConflictError):
        update_task_details(session, task_id=parent.id, context_id=None)
    assert child.context_id == context_a.id


def test_resource_unlink_allows_explicit_context_change(session, context_a, context_b):
    resource = make_resource(session, context_a)
    task = create_task(session, title="Uebung", context_id=context_a.id)
    link_task_resource(session, task_id=task.id, resource_id=resource.id)
    unlink_task_resource(session, task_id=task.id, resource_id=resource.id)
    update_task_details(session, task_id=task.id, context_id=context_b.id)
    assert task.context_id == context_b.id
