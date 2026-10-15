"""The workspace HTTP endpoint and the compatibility of the existing packets.

Two things are pinned here:

* the router factory behaves as a *factory* -- no auth of its own, mounted into the
  application's protected router, one read-only route, no budget knob in the request;
* the existing ``ContextPacket`` and the planner prompt keep working unchanged when
  no knowledge store is passed, and gain a bounded, metadata-only workspace excerpt
  when one is.
"""

from __future__ import annotations

import json

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from organiser.api.security import build_api_token_dependency
from organiser.api.workspace import build_router
from organiser.core.config import Settings
from organiser.db.session import create_session_factory
from organiser.services.context import planning_context, project_context, task_context
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    NotFoundError,
    create_project,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.planner import build_planner_prompt
from organiser.services.workspace_context import DEFAULT_WORKSPACE_POLICY
from support.workspace import (
    add_context,
    add_open_task,
    finance as finance,
    store as store,
    workspace_env as workspace_env,
)
from conftest import TEST_API_TOKEN, AuthenticatedTestClient


@pytest.fixture
def client(workspace_env, store, tmp_path):
    """A protected test app with the workspace router mounted, exactly as the
    integrator will mount it: the bearer dependency lives on the parent router."""
    session, _tmp = workspace_env
    settings = Settings(database_path=tmp_path / "workspace.sqlite3", api_token=TEST_API_TOKEN)
    app = FastAPI()

    def get_session():
        with create_session_factory(settings)() as own:
            yield own

    def get_store() -> KnowledgeStore:
        return store

    # The same domain-error mapping the production app installs, so the status codes
    # observed here are the ones a client will actually see.
    @app.exception_handler(NotFoundError)
    async def not_found_handler(_, exc: NotFoundError):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(_, exc: ConflictError):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainValidationError)
    async def validation_handler(_, exc: DomainValidationError):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    protected = APIRouter(dependencies=[Depends(build_api_token_dependency(settings))])
    protected.include_router(build_router(get_session, get_store))
    app.include_router(protected)
    return AuthenticatedTestClient(app)


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


def test_the_endpoint_requires_the_bearer_token(client, finance):
    unauthorised = TestClient(client.app)

    assert unauthorised.get(f"/contexts/{finance['context'].id}/workspace").status_code == 401
    assert client.get(f"/contexts/{finance['context'].id}/workspace").status_code == 200


def test_the_endpoint_returns_the_bounded_packet(client, finance):
    response = client.get(f"/contexts/{finance['context'].id}/workspace")

    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "context_workspace"
    assert body["evidence_policy"] == "untrusted_data_not_instructions"
    assert body["context"]["id"] == finance["context"].id
    assert len(body["current_tasks"]) == 2
    assert len(body["knowledge_excerpts"]) <= DEFAULT_WORKSPACE_POLICY.max_excerpts
    assert body["manifest"]["version"] == 1
    assert body["freshness"]["authoritative"] is False
    # The whole response is inside the documented packet budget.
    assert (
        len(json.dumps(body, separators=(",", ":")))
        <= DEFAULT_WORKSPACE_POLICY.max_packet_chars + 64
    )


def test_an_unknown_context_is_a_404(client):
    assert client.get("/contexts/gibt-es-nicht/workspace").status_code == 404


def test_focus_ids_may_be_passed_and_are_validated(client, finance):
    context_id = finance["context"].id

    focused = client.get(
        f"/contexts/{context_id}/workspace", params={"topic_id": [finance["blocked"].id]}
    )
    assert focused.status_code == 200
    assert focused.json()["selection"]["topic_ids"] == [finance["blocked"].id]
    assert focused.json()["topics"][0]["id"] == finance["blocked"].id

    foreign = client.get(
        f"/contexts/{context_id}/workspace", params={"topic_id": ["gibt-es-nicht"]}
    )
    assert foreign.status_code == 422
    assert "unbekannte ID" in foreign.json()["detail"]


def test_the_request_cannot_widen_the_budget(client, finance):
    response = client.get(
        f"/contexts/{finance['context'].id}/workspace",
        params={"max_packet_chars": 10**9, "max_excerpt_chars": 999_999, "max_tasks": 500},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body["current_tasks"]) <= DEFAULT_WORKSPACE_POLICY.max_tasks
    assert all(
        len(excerpt["text"]) <= DEFAULT_WORKSPACE_POLICY.max_excerpt_chars
        for excerpt in body["knowledge_excerpts"]
    )
    assert (
        len(json.dumps(body, separators=(",", ":")))
        <= DEFAULT_WORKSPACE_POLICY.max_packet_chars + 64
    )


def test_the_endpoint_never_accepts_a_read_path(client, finance):
    """No absolute or relative path may be smuggled in as a read target."""
    for parameter in ("path", "root", "file", "knowledge_path", "locator"):
        response = client.get(
            f"/contexts/{finance['context'].id}/workspace", params={parameter: "/etc/passwd"}
        )
        # The parameter is not part of the contract, so it is simply ignored.
        assert response.status_code == 200
        assert "passwd" not in json.dumps(response.json())


def _business_routes(app: FastAPI) -> dict[str, set[str]]:
    """Path -> HTTP methods of the mounted business routes.

    Read from the generated OpenAPI schema, which is the public contract of what the
    app serves. Walking ``app.routes`` would depend on how this FastAPI version
    happens to represent an included router.
    """
    return {
        path: set(operations)
        for path, operations in app.openapi()["paths"].items()
        if not path.startswith(("/openapi", "/docs", "/redoc"))
    }


def test_the_endpoint_exposes_no_llm_or_write_route(client, finance):
    business = _business_routes(client.app)

    assert set(business) == {"/contexts/{context_id}/workspace"}
    assert all(methods == {"get"} for methods in business.values())


def test_the_endpoint_writes_nothing(client, finance):
    from support.workspace import database_fingerprint, tree_fingerprint

    database = finance["tmp_path"] / "workspace.sqlite3"
    before_tree = tree_fingerprint(finance["store"].root)
    before_db = database_fingerprint(database)

    for _ in range(2):
        assert client.get(f"/contexts/{finance['context'].id}/workspace").status_code == 200

    assert tree_fingerprint(finance["store"].root) == before_tree
    assert database_fingerprint(database) == before_db


# ---------------------------------------------------------------------------
# Compatibility of the existing packets
# ---------------------------------------------------------------------------


def test_task_context_is_unchanged_without_a_store(finance):
    """No store means no workspace field value: the packet stays as it was."""
    packet = task_context(finance["session"], finance["task"].id)

    assert packet.workspace is None
    assert "workspace" not in packet.model_dump(exclude_none=True)


def test_planning_context_works_without_a_store(finance):
    packet = planning_context(finance["session"], target_type="task", target_id=finance["task"].id)

    assert packet.kind == "planning_context"
    assert packet.target_id == finance["task"].id
    assert packet.workspace is None
    # The field is present but null, so the prompt carries no workspace content at
    # all: byte-identical to the prompt a caller received before this change.
    assert '"workspace":null' in build_planner_prompt(packet)


def test_planning_context_carries_the_small_workspace_excerpt(finance):
    packet = planning_context(
        finance["session"], target_type="task", target_id=finance["task"].id, store=finance["store"]
    )

    assert packet.kind == "planning_context"
    assert packet.workspace is not None
    assert packet.workspace.context_id == finance["context"].id
    assert packet.workspace.open_task_count == 2
    assert {topic.id for topic in packet.workspace.topics} == {
        finance["balance"].id,
        finance["blocked"].id,
    }
    prompt = build_planner_prompt(packet)
    assert '"workspace":null' not in prompt
    assert packet.workspace.context_id in prompt
    # Still metadata only: no untrusted document or markdown text in the planner prompt.
    assert "Die Bewertung von Vermoegensgegenstaenden" not in prompt
    assert "Aktiva und Passiva" not in prompt


def test_a_project_bound_task_gets_no_guessed_workspace(finance):
    project = create_project(finance["session"], title="Finanzierung und Rechnungswesen")
    task = add_open_task(
        finance["session"], title="Projektaufgabe", project_id=project.id, context=None
    )

    packet = task_context(finance["session"], task.id, store=finance["store"])

    assert packet.workspace is None
    assert packet.project["id"] == project.id


def test_the_parent_binding_is_visible_on_a_child_task(finance):
    child = add_open_task(
        finance["session"], context=None, title="Kindaufgabe", parent_task_id=finance["task"].id
    )

    packet = task_context(finance["session"], child.id, store=finance["store"])

    assert packet.parent_task["id"] == finance["task"].id
    assert packet.parent_task["context_id"] == finance["context"].id
    assert packet.parent_task["topic_id"] == finance["balance"].id
    # The child itself has no context binding, so it gets no workspace.
    assert packet.workspace is None


def test_project_context_is_unchanged(finance):
    project = create_project(finance["session"], title="Hausarbeit")
    add_open_task(finance["session"], title="Kapitel", project_id=project.id)

    packet = project_context(finance["session"], project.id)

    assert packet.kind == "project_context"
    assert packet.workspace is None
    assert [task["title"] for task in packet.related_tasks] == ["Kapitel"]


def test_the_existing_task_shape_survives_a_context_binding(workspace_env):
    session, _tmp = workspace_env
    store = KnowledgeStore(_tmp / "knowledge")
    store.root.mkdir()
    context = add_context(session, title="Kurs")
    task = add_open_task(session, context=context, title="Mit Kontext")

    packet = task_context(session, task.id, store=store)

    assert packet.target["id"] == task.id
    assert packet.target["title"] == "Mit Kontext"
    assert packet.workspace.context_id == context.id
    # The pre-existing fields are all still there and unchanged in type.
    assert isinstance(packet.child_tasks, list)
    assert isinstance(packet.recent_events, list)
    assert set(packet.selection) == {"max_child_tasks", "max_related_tasks", "max_recent_events"}
