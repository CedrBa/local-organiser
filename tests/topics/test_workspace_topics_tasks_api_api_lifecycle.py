"""Api lifecycle contracts for topics."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from conftest import auth_headers
from organiser.api.schemas import (
    TaskCreate,
    TaskUpdate,
)
from organiser.services.domain import create_task, list_events_for_entity
from support.workspace_topics_tasks_api import (
    harness as harness,
    ROUTES as ROUTES,
    topic_file_for as topic_file_for,
)


@pytest.mark.parametrize("method,path,body", ROUTES)
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer wrong"}])
def test_all_c_routes_inherit_auth(harness, method, path, body, headers):
    client, ids, _, store, _ = harness
    response = client.request(method, path.format(**ids), json=body, headers=headers)
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert not store.root.exists()


@pytest.mark.parametrize("harness", [None], indirect=True)
@pytest.mark.parametrize("method,path,body", ROUTES)
def test_all_c_routes_fail_closed_without_token(harness, method, path, body):
    client, ids, _, store, _ = harness
    response = client.request(method, path.format(**ids), json=body, headers=auth_headers())
    assert response.status_code == 503
    assert not store.root.exists()


def test_auth_matrix_covers_every_factory_route(harness):
    _, _, _, _, router = harness
    actual = {(method, route.path) for route in router.routes for method in route.methods}
    # Matrix placeholders differ from FastAPI parameter names, normalize them.
    expected = {
        (
            method,
            path.replace("{context}", "{context_id}")
            .replace("{topic}", "{topic_id}")
            .replace("{task}", "{task_id}")
            .replace("{resource}", "{resource_id}"),
        )
        for method, path, _ in ROUTES
    }
    assert actual == expected


def test_topic_lifecycle_and_explicit_knowledge(harness):
    client, ids, _, store, _ = harness
    headers = auth_headers()
    created = client.post(
        f"/contexts/{ids['context']}/topics", json={"title": "𝐀"}, headers=headers
    )
    assert created.status_code == 201
    topic = created.json()
    assert topic["title_key"] == "a" and topic["knowledge_key"] == f"topic-{topic['id']}"
    assert not store.root.exists()
    duplicate = client.post(
        f"/contexts/{ids['context']}/topics", json={"title": "A"}, headers=headers
    )
    assert duplicate.status_code == 409
    rename = client.patch(f"/topics/{topic['id']}", json={"title": "Abschreibung"}, headers=headers)
    assert rename.status_code == 200 and rename.json()["knowledge_key"] == topic["knowledge_key"]
    status = client.post(f"/topics/{topic['id']}/status", json={"status": "done"}, headers=headers)
    assert status.status_code == 200 and status.json()["status"] == "done"
    ensure = client.post(f"/topics/{topic['id']}/knowledge/ensure", headers=headers)
    assert ensure.status_code == 200 and ensure.json()["created"]
    repeat = client.post(f"/topics/{topic['id']}/knowledge/ensure", headers=headers)
    assert repeat.status_code == 200 and not repeat.json()["created"]
    update = client.post(
        f"/topics/{topic['id']}/knowledge",
        json={"text": "Lernnotiz", "percent": 75},
        headers=headers,
    )
    assert update.status_code == 200 and update.json()["changed"]
    assert len(update.json()["content_hash"]) == 64
    assert client.get(f"/topics/{topic['id']}", headers=headers).json()["status"] == "done"
    assert str(store.root) not in update.text


@pytest.mark.parametrize(
    "body", [{"title": None}, {"knowledge_key": "other"}, {"context_id": "other"}, {"percent": 75}]
)
def test_topic_patch_rejects_null_title_and_immutable_or_unknown_fields(harness, body):
    client, ids, _, _, _ = harness
    assert (
        client.patch(f"/topics/{ids['topic']}", json=body, headers=auth_headers()).status_code
        == 422
    )


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"path": "../escape", "text": "x"},
        {"status": "done", "text": "x"},
        {"percent": True},
        {"percent": 1.5},
        {"percent": "75"},
        {"text": "## Injection"},
    ],
)
def test_knowledge_inputs_fail_without_creating_files(harness, body):
    client, ids, _, store, _ = harness
    response = client.post(f"/topics/{ids['topic']}/knowledge", json=body, headers=auth_headers())
    assert response.status_code == 422
    assert not store.root.exists()


def test_missing_entities_and_uninitialized_knowledge_are_controlled(harness):
    client, ids, _, _, _ = harness
    assert client.get("/topics/missing", headers=auth_headers()).status_code == 404
    assert (
        client.post("/topics/missing/knowledge/ensure", headers=auth_headers()).status_code == 404
    )
    response = client.post(
        f"/topics/{ids['topic']}/knowledge", json={"text": "x"}, headers=auth_headers()
    )
    assert (
        response.status_code == 409
        and response.json()["detail"]["code"] == "knowledge_not_initialized"
    )


def test_links_use_ids_json_and_preserve_provenance(harness):
    client, ids, factory, _, _ = harness
    body = {"resource_id": ids["resource"], "event_context": {"source_id": "proposal-1"}}
    path = f"/tasks/{ids['task']}/resources"
    first = client.post(path, json=body, headers=auth_headers())
    assert first.status_code == 201
    assert client.post(path + f"/{ids['resource']}", headers=auth_headers()).status_code == 201
    listed = client.get(path, headers=auth_headers()).json()
    assert [item["id"] for item in listed] == [ids["resource"]]
    assert "/private" not in str(listed)
    with factory() as session:
        events = list_events_for_entity(session, entity_type="task", entity_id=ids["task"])
        links = [event for event in events if event.event_type == "TASK_RESOURCE_LINKED"]
        assert len(links) == 1 and links[0].source_id == "proposal-1"
    for _ in range(2):
        assert (
            client.delete(path + f"/{ids['resource']}", headers=auth_headers()).status_code == 204
        )
    assert client.get(path, headers=auth_headers()).json() == []


def test_topic_resource_roundtrip_and_context_ensure(harness):
    client, ids, factory, _, _ = harness
    headers = auth_headers()
    base = f"/topics/{ids['topic']}/resources"
    target = base + f"/{ids['resource']}"
    assert client.get(base, headers=headers).json() == []
    for _ in range(2):
        assert (
            client.post(target, json={"source_id": "proposal-link"}, headers=headers).status_code
            == 201
        )
    listed = client.get(base, headers=headers).json()
    assert [item["id"] for item in listed] == [ids["resource"]]
    assert "/private" not in str(listed)
    with factory() as session:
        events = list_events_for_entity(session, entity_type="topic", entity_id=ids["topic"])
        links = [event for event in events if event.event_type == "RESOURCE_TOPIC_LINKED"]
        assert len(links) == 1 and links[0].source_id == "proposal-link"
    for _ in range(2):
        assert client.delete(target, headers=headers).status_code == 200
    assert client.get(base, headers=headers).json() == []
    topics = client.get(f"/contexts/{ids['context']}/topics", headers=headers)
    assert topics.status_code == 200 and [topic["id"] for topic in topics.json()] == [ids["topic"]]
    assert (
        client.get(f"/contexts/{ids['context']}/topics?status=done", headers=headers).json() == []
    )
    ensure = client.post(f"/contexts/{ids['context']}/knowledge/ensure", headers=headers)
    assert ensure.status_code == 200 and ensure.json()["operation_id"]
    assert (
        client.post(f"/contexts/{ids['context']}/knowledge/ensure", headers=headers).json()[
            "changed"
        ]
        is False
    )


@pytest.mark.parametrize("entity", ["topic", "task"])
@pytest.mark.parametrize("method", ["POST", "DELETE"])
def test_cross_context_resource_operations_conflict(harness, entity, method):
    client, ids, _, _, _ = harness
    response = client.request(
        method,
        f"/{entity}s/{ids[entity]}/resources/{ids['foreign_resource']}",
        headers=auth_headers(),
    )
    assert response.status_code == 409


@pytest.mark.parametrize("operation", ["context", "topic"])
def test_knowledge_errors_do_not_leak_paths(harness, monkeypatch, operation):
    client, ids, _, store, _ = harness

    def denied(*args, **kwargs):
        raise PermissionError("private data: " + str(store.root))

    monkeypatch.setattr(store._filesystem, "atomic_write", denied)
    path = (
        f"/contexts/{ids['context']}/knowledge/ensure"
        if operation == "context"
        else f"/topics/{ids['topic']}/knowledge/ensure"
    )
    response = client.post(path, headers=auth_headers())
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "knowledge_operation_failed"}
    assert str(store.root) not in response.text and "PermissionError" not in response.text


def test_late_topic_write_failure_is_visible_as_needs_review(harness, monkeypatch):
    client, ids, _, store, _ = harness
    assert (
        client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers()).status_code
        == 200
    )

    def denied(*args, **kwargs):
        raise OSError("private data: " + str(store.root))

    monkeypatch.setattr(store._filesystem, "atomic_write", denied)
    response = client.post(
        f"/topics/{ids['topic']}/knowledge", json={"text": "x"}, headers=auth_headers()
    )
    assert response.status_code == 409
    assert response.json()["detail"] == {"code": "knowledge_ambiguous", "needs_review": True}
    assert str(store.root) not in response.text


def test_task_schemas_preserve_missing_null_and_required_field_rules(harness):
    _, ids, factory, _, _ = harness
    missing = TaskCreate(title="Inherited", parent_task_id=ids["task"])
    explicit = TaskCreate(title="Unlinked", parent_task_id=ids["task"], context_id=None)
    assert "context_id" not in missing.model_fields_set
    assert explicit.model_dump(exclude_unset=True)["context_id"] is None
    with factory() as session:
        inherited = create_task(
            session, **missing.model_dump(exclude_unset=True, exclude={"event_context"})
        )
        unlinked = create_task(
            session, **explicit.model_dump(exclude_unset=True, exclude={"event_context"})
        )
        assert inherited.context_id == ids["context"] and unlinked.context_id is None
    assert TaskUpdate(context_id=None, topic_id=None).model_dump(exclude_unset=True) == {
        "context_id": None,
        "topic_id": None,
    }
    for field in ("title", "allow_split", "cognitive_load"):
        with pytest.raises(ValidationError):
            TaskUpdate(**{field: None})
