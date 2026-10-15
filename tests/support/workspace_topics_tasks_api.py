"""Shared fixtures and builders for test_workspace_topics_tasks_api contracts; no executable tests."""

from __future__ import annotations

import pytest
from dataclasses import replace
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient
from conftest import TEST_API_TOKEN, auth_headers, make_settings
from organiser.api.app import create_app
from organiser.api.security import build_api_token_dependency
from organiser.api.topics import build_router
from organiser.db.models import Resource, new_id
from organiser.db.session import create_session_factory
from organiser.services.domain import create_context, create_task
from organiser.services.context_knowledge import shared_knowledge_store
from organiser.services.topics import create_topic


@pytest.fixture
def harness(tmp_path, request):
    settings = replace(
        make_settings(tmp_path, "router.sqlite3", knowledge_root=tmp_path / "knowledge"),
        api_token=getattr(request, "param", TEST_API_TOKEN),
    )
    factory = create_session_factory(settings)
    # create_app mountet die Topic-Routen inzwischen selbst mit dem gemeinsamen Store. Das Harness muss
    # denselben Store benutzen, sonst gewinnt die zuerst registrierte App-Route und Monkeypatches greifen ins Leere.
    store = shared_knowledge_store(settings)

    def get_session():
        with factory() as session:
            yield session

    app = create_app(settings)
    protected = APIRouter(dependencies=[Depends(build_api_token_dependency(settings))])
    router = build_router(get_session, lambda: store)
    protected.include_router(router)
    app.include_router(protected)
    with TestClient(app) as client:
        with factory() as session:
            context = create_context(session, title="Finanzierung", type="course")
            other = create_context(session, title="Finanzierung", type="course")
            topic = create_topic(session, context_id=context.id, title="Bilanzierung")
            task = create_task(session, title="Uebung", context_id=context.id)
            resources = []
            for owner in (context, other):
                resource = Resource(
                    context_id=owner.id,
                    locator=f"/private/{new_id()}",
                    relative_path="uebung.md",
                    source_path_snapshot="/private/source",
                    title="Uebung",
                    resource_type="markdown",
                )
                session.add(resource)
                session.flush()
                resources.append(resource.id)
            session.commit()
            ids = {
                "context": context.id,
                "topic": topic.id,
                "task": task.id,
                "resource": resources[0],
                "foreign_resource": resources[1],
            }
        yield client, ids, factory, store, router


ROUTES = [
    ("GET", "/contexts/{context}/topics", None),
    ("POST", "/contexts/{context}/topics", {"title": "Neu"}),
    ("GET", "/topics/{topic}", None),
    ("PATCH", "/topics/{topic}", {"title": "Neu"}),
    ("POST", "/topics/{topic}/status", {"status": "done"}),
    ("GET", "/topics/{topic}/resources", None),
    ("POST", "/topics/{topic}/resources/{resource}", None),
    ("DELETE", "/topics/{topic}/resources/{resource}", None),
    ("GET", "/tasks/{task}/resources", None),
    ("POST", "/tasks/{task}/resources", {"resource_id": "id"}),
    ("POST", "/tasks/{task}/resources/{resource}", None),
    ("DELETE", "/tasks/{task}/resources/{resource}", None),
    ("POST", "/contexts/{context}/knowledge/ensure", None),
    ("POST", "/topics/{topic}/knowledge/ensure", None),
    ("POST", "/topics/{topic}/knowledge", {"text": "Fortschritt"}),
]

# -- review follow-up: a user-fixable file is never "retry later" (F5) ----------


def topic_file_for(harness, ids):
    client, _, _, store, _ = harness
    assert (
        client.post(f"/topics/{ids['topic']}/knowledge/ensure", headers=auth_headers()).status_code
        == 200
    )
    return store.root / "contexts" / ids["context"] / "topics" / f"topic-{ids['topic']}.md"
