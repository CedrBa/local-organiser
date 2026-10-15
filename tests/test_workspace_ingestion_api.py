"""HTTP surface of the resource registry.

The router is a factory: the integrator mounts it into the existing protected
router. These tests therefore mount it themselves, behind an explicit test token
dependency, and assert exactly two things about that boundary:

* without the token nothing is reachable -- the router carries no auth of its own
  and is not a back door around the application's Bearer-token dependency;
* with the token every route works and every refusal carries a stable code.

No request may carry a filesystem path, and the responses carry no absolute
locator: relative labels, ids, hashes and codes only.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.testclient import TestClient
from sqlalchemy import select

from organiser.api.resources import build_router
from organiser.db.models import Event, ProcessingRun, Resource, ResourceSnapshot
from organiser.db.session import create_session_factory
from organiser.domain.resources import ScanResult
from support.ingestion import (
    make_pdf_bytes,
    make_settings,
    seed_context,
    seed_source_id,
    session_for,
)

TEST_TOKEN = "test-api-token"


def build_client(settings) -> tuple[TestClient, callable]:
    """Mount the router behind an explicit test token, like the integrator will."""
    session_factory = create_session_factory(settings)
    security = HTTPBearer(auto_error=False)

    def get_session():
        with session_factory() as session:
            yield session

    def require_token(credentials: HTTPAuthorizationCredentials | None = Depends(security)):
        if credentials is None or credentials.credentials != TEST_TOKEN:
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="unauthorized")

    app = FastAPI()
    app.include_router(build_router(get_session, settings), dependencies=[Depends(require_token)])
    return TestClient(app), session_factory


@pytest.fixture
def course(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("erste Fassung", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,), api_token=TEST_TOKEN)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        session.rollback()
        source_id = seed_source_id(session, context_id, path=str(root))
    return settings, context_id, source_id, root


@pytest.fixture
def client(course):
    settings = course[0]
    test_client, _factory = build_client(settings)
    return test_client


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}"}


# -- authentication ----------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/contexts/{context_id}/resources"),
        ("get", "/resources/{resource_id}"),
        ("post", "/contexts/{context_id}/sources/{source_id}/scan"),
        ("post", "/resources/{resource_id}/extract"),
        ("post", "/resources/{resource_id}/verify"),
    ],
)
def test_every_route_requires_the_token(course, method, path):
    settings, context_id, source_id, _root = course
    test_client, _factory = build_client(settings)
    response = getattr(test_client, method)(
        path.format(context_id=context_id, source_id=source_id, resource_id="egal")
    )
    assert response.status_code == 401


# -- happy paths -------------------------------------------------------------


def test_scan_then_list_then_read_snapshot(client, course):
    settings, context_id, source_id, _root = course
    scan = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert scan.status_code == 200
    body = scan.json()
    assert body["status"] == "completed"
    assert body["complete"] is True
    assert body["counters"]["created"] == 1
    assert body["processing_run_id"]

    listing = client.get(f"/contexts/{context_id}/resources", headers=auth())
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    item = listing.json()["items"][0]
    assert item["relative_path"] == "Notiz.md"
    assert item["resource_type"] == "markdown"
    assert item["status"] == "active"
    resource_id = item["id"]

    detail = client.get(f"/resources/{resource_id}", headers=auth())
    assert detail.status_code == 200
    payload = detail.json()
    assert payload["current_snapshot"]["text"] == "erste Fassung"
    assert payload["snapshot_is_stale"] is False
    assert payload["snapshots"][0]["extractor_version"] == "text-utf8-1.0.0"

    extraction = client.post(f"/resources/{resource_id}/extract", headers=auth())
    assert extraction.status_code == 200
    assert extraction.json()["reused_existing_snapshot"] is True

    verify = client.post(
        f"/resources/{resource_id}/verify",
        headers=auth(),
        json={"expected_hash": payload["current_snapshot"]["content_hash"]},
    )
    assert verify.status_code == 200
    assert verify.json()["ok"] is True


def test_responses_never_contain_an_absolute_address(client, course):
    settings, context_id, source_id, root = course
    client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    listing = client.get(f"/contexts/{context_id}/resources", headers=auth())
    resource_id = listing.json()["items"][0]["id"]
    detail = client.get(f"/resources/{resource_id}", headers=auth())
    for response in (listing, detail):
        assert str(root) not in response.text
    assert "locator" not in listing.text


def test_listing_is_paged_and_bounded(client, course):
    settings, context_id, source_id, root = course
    for index in range(3):
        (root / f"Notiz{index}.md").write_text("x", encoding="utf-8")
    client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    page = client.get(f"/contexts/{context_id}/resources?limit=2", headers=auth())
    assert page.status_code == 200
    assert page.json()["total"] == 4
    assert len(page.json()["items"]) == 2
    assert (
        client.get(f"/contexts/{context_id}/resources?limit=0", headers=auth()).status_code == 422
    )
    assert (
        client.get(f"/contexts/{context_id}/resources?limit=5000", headers=auth()).status_code
        == 422
    )
    assert (
        client.get(f"/contexts/{context_id}/resources?status=kaputt", headers=auth()).status_code
        == 422
    )
    assert (
        client.get(f"/contexts/{context_id}/resources?status=missing", headers=auth()).json()[
            "total"
        ]
        == 0
    )


# -- refusals carry codes ----------------------------------------------------


def test_unknown_context_is_404_with_a_code(client):
    response = client.get("/contexts/gibt-es-nicht/resources", headers=auth())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "context_not_found"


def test_unknown_resource_is_404_with_a_code(client):
    response = client.get("/resources/gibt-es-nicht", headers=auth())
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "resource_not_found"


def test_cross_context_source_is_refused(course):
    settings, context_id, source_id, _root = course
    with session_for(settings) as session:
        other_context = seed_context(session).id
        session.rollback()
    test_client, _factory = build_client(settings)
    response = test_client.post(
        f"/contexts/{other_context}/sources/{source_id}/scan", headers=auth()
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "source_not_found"


def test_scan_of_a_denied_source_is_403_with_a_code(tmp_path):
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Notiz.md").write_text("x", encoding="utf-8")
    settings = make_settings(tmp_path, allowed_roots=(), api_token=TEST_TOKEN)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        session.rollback()
        source_id = seed_source_id(session, context_id, path=str(root))
    test_client, _factory = build_client(settings)
    response = test_client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "source_not_allowed"
    assert response.json()["detail"]["reason"] == "no_allowed_roots_configured"


def test_scan_of_a_relative_source_is_422_with_a_code(tmp_path):
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,), api_token=TEST_TOKEN)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        session.rollback()
        source_id = seed_source_id(session, context_id, path="relativ/pfad")
    test_client, _factory = build_client(settings)
    response = test_client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "source_path_not_absolute"


def test_verify_body_forbids_extra_fields(client, course):
    settings, context_id, source_id, _root = course
    client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    resource_id = client.get(f"/contexts/{context_id}/resources", headers=auth()).json()["items"][
        0
    ]["id"]
    response = client.post(
        f"/resources/{resource_id}/verify",
        headers=auth(),
        json={"expected_hash": "0" * 64, "path": "/etc/passwd"},
    )
    assert response.status_code == 422
    response = client.post(
        f"/resources/{resource_id}/verify", headers=auth(), json={"expected_hash": "kein-hash"}
    )
    assert response.status_code == 422


def test_refusals_never_leak_a_raw_exception_text(client, course):
    settings, context_id, source_id, _root = course
    response = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert response.status_code == 200
    response = client.get("/resources/gibt-es-nicht", headers=auth())
    body = response.json()["detail"]
    assert set(body) <= {"code", "detail", "source_id", "context_id", "resource_id", "reason"}
    assert "Traceback" not in response.text
    assert "/tmp/" not in response.text


# -- audit and side effects --------------------------------------------------


def test_scan_writes_one_run_and_audited_events(client, course):
    settings, context_id, source_id, _root = course
    response = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    run_id = response.json()["processing_run_id"]
    with session_for(settings) as session:
        run = session.get(ProcessingRun, run_id)
        assert run.run_type == "resource_scan"
        assert run.status == "completed"
        assert run.output_payload["source_id"] == source_id
        events = [event.event_type for event in session.scalars(select(Event))]
        assert "RESOURCE_REGISTERED" in events
        assert "RESOURCE_EXTRACTED" in events
        assert "CONTEXT_SOURCE_SCANNED" in events
        assert len(list(session.scalars(select(Resource)))) == 1
        assert len(list(session.scalars(select(ResourceSnapshot)))) == 1


def test_repeated_scan_over_http_is_idempotent(client, course):
    settings, context_id, source_id, _root = course
    client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    second = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert second.status_code == 200
    assert second.json()["counters"]["created"] == 0
    with session_for(settings) as session:
        assert len(list(session.scalars(select(Resource)))) == 1
        assert len(list(session.scalars(select(ResourceSnapshot)))) == 1


def test_pdf_scan_and_extract_over_http(tmp_path):
    pytest.importorskip("pypdf")
    root = tmp_path / "kurs"
    root.mkdir()
    (root / "Vorlesung.pdf").write_bytes(make_pdf_bytes(["Bilanzierung"]))
    settings = make_settings(tmp_path, allowed_roots=(tmp_path,), api_token=TEST_TOKEN)
    with session_for(settings) as session:
        context_id = seed_context(session).id
        session.rollback()
        source_id = seed_source_id(session, context_id, path=str(root))
    test_client, _factory = build_client(settings)
    scan = test_client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth())
    assert scan.status_code == 200
    resource_id = test_client.get(f"/contexts/{context_id}/resources", headers=auth()).json()[
        "items"
    ][0]["id"]
    detail = test_client.get(f"/resources/{resource_id}", headers=auth()).json()
    assert detail["current_snapshot"]["status"] == "ready"
    assert "Bilanzierung" in detail["current_snapshot"]["text"]
    assert detail["current_snapshot"]["metadata_payload"]["pages"][0]["page"] == 1
    assert detail["current_snapshot"]["page_count"] == 1


def test_scan_result_is_typed_and_complete(client, course):
    settings, context_id, source_id, _root = course
    body = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=auth()).json()
    assert ScanResult.model_validate(body) == ScanResult.model_validate(body)
    assert set(body["counters"]) == {
        "observed",
        "created",
        "refreshed",
        "unchanged",
        "marked_missing",
        "skipped_unsafe",
        "skipped_excluded",
        "skipped_unsupported_type",
        "duplicates",
        "entries",
        "rebound",
    }
