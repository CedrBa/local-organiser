"""Api contracts for contexts."""

from __future__ import annotations

import pytest
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


def test_api_creates_context_without_source_and_reads_it_back(client):
    response = client.post(
        "/contexts", json={"title": "Finanzierung und Rechnungswesen", "type": "topic"}
    )

    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "active"
    assert created["parent_context_id"] is None
    assert created["knowledge_path"] is None
    assert without_timestamps(client.get("/contexts").json()[0]) == without_timestamps(created)
    assert without_timestamps(
        client.get(f"/contexts/{created['id']}").json()
    ) == without_timestamps(created)
    assert client.get(f"/contexts/{created['id']}/sources").json() == []
    assert [
        event["event_type"] for event in client.get(f"/contexts/{created['id']}/events").json()
    ] == ["CONTEXT_CREATED"]


def test_api_requires_authentication_for_context_routes(client):
    assert client.get("/contexts", headers={"Authorization": "Bearer falsch"}).status_code == 401
    assert (
        client.post(
            "/contexts",
            json={"title": "Bewerbungen", "type": "topic"},
            headers={"Authorization": ""},
        ).status_code
        == 401
    )


def test_api_manages_parent_child_contexts(client):
    applications = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()
    sap = client.post(
        "/contexts",
        json={"title": "SAP", "type": "application", "parent_context_id": applications["id"]},
    ).json()
    schwarz = client.post(
        "/contexts",
        json={
            "title": "Schwarz Gruppe",
            "type": "application",
            "parent_context_id": applications["id"],
        },
    ).json()

    assert [
        item["id"] for item in client.get(f"/contexts/{applications['id']}/children").json()
    ] == [sap["id"], schwarz["id"]]
    assert [item["id"] for item in client.get("/contexts?roots_only=true").json()] == [
        applications["id"]
    ]
    assert [item["id"] for item in client.get("/contexts?type=application").json()] == [
        sap["id"],
        schwarz["id"],
    ]

    detached = client.patch(f"/contexts/{sap['id']}", json={"parent_context_id": None}).json()
    assert detached["parent_context_id"] is None
    assert [
        item["id"] for item in client.get(f"/contexts/{applications['id']}/children").json()
    ] == [schwarz["id"]]


def test_api_reports_invalid_parent_as_error(client):
    applications = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()

    assert (
        client.post(
            "/contexts", json={"title": "SAP", "type": "application", "parent_context_id": "fehlt"}
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/contexts/{applications['id']}", json={"parent_context_id": applications["id"]}
        ).status_code
        == 422
    )
    assert client.post("/contexts", json={"title": "   ", "type": "topic"}).status_code == 422
    assert client.get("/contexts/fehlt").status_code == 404
    assert client.get("/contexts/fehlt/children").status_code == 404


def test_api_registers_and_reads_multiple_sources(client):
    created = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()
    lecture = client.post(
        f"/contexts/{created['id']}/sources",
        json={"source_type": "document", "path": "uni/finanzierung.pdf"},
    )
    note = client.post(
        f"/contexts/{created['id']}/sources",
        json={"source_type": "note", "path": "notizen/finanzierung.md"},
    )

    assert lecture.status_code == 201
    assert note.status_code == 201
    assert lecture.json()["status"] == "active"
    sources = client.get(f"/contexts/{created['id']}/sources").json()
    assert [source["path"] for source in sources] == [
        "uni/finanzierung.pdf",
        "notizen/finanzierung.md",
    ]

    duplicate = client.post(
        f"/contexts/{created['id']}/sources",
        json={"source_type": "document", "path": "uni/finanzierung.pdf"},
    )
    assert duplicate.status_code == 409
    assert (
        client.post(
            f"/contexts/{created['id']}/sources",
            json={"source_type": "scanner", "path": "uni/scanner"},
        ).status_code
        == 422
    )
    assert (
        client.get(f"/contexts/{created['id']}/sources/{lecture.json()['id']}").json()["path"]
        == "uni/finanzierung.pdf"
    )


def test_api_updates_and_deletes_source(client):
    created = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()
    source = client.post(
        f"/contexts/{created['id']}/sources",
        json={"source_type": "document", "path": "uni/alt.pdf"},
    ).json()

    updated = client.patch(
        f"/contexts/{created['id']}/sources/{source['id']}",
        json={"status": "missing", "path": "uni/neu.pdf"},
    ).json()
    assert updated["status"] == "missing"
    assert updated["path"] == "uni/neu.pdf"
    assert (
        client.patch(
            f"/contexts/{created['id']}/sources/{source['id']}", json={"status": None}
        ).status_code
        == 422
    )

    assert client.delete(f"/contexts/{created['id']}/sources/{source['id']}").status_code == 204
    assert client.get(f"/contexts/{created['id']}/sources").json() == []
    assert client.delete(f"/contexts/{created['id']}/sources/{source['id']}").status_code == 404
    assert client.get(f"/contexts/{created['id']}/sources/{source['id']}").status_code == 404
    assert client.get(f"/contexts/{created['id']}").json()["title"] == created["title"]


def test_api_changes_context_status(client):
    created = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()

    assert (
        client.post(f"/contexts/{created['id']}/status", json={"status": "on_hold"}).json()[
            "status"
        ]
        == "on_hold"
    )
    assert (
        client.post(f"/contexts/{created['id']}/status", json={"status": "completed"}).json()[
            "status"
        ]
        == "completed"
    )
    assert (
        client.post(f"/contexts/{created['id']}/status", json={"status": "on_hold"}).status_code
        == 409
    )
    assert (
        client.post(f"/contexts/{created['id']}/status", json={"status": "archived"}).status_code
        == 422
    )
    assert (
        client.post(f"/contexts/{created['id']}/status", json={"status": "active"}).json()["status"]
        == "active"
    )
    assert [item["id"] for item in client.get("/contexts?status=active").json()] == [created["id"]]
    assert client.get("/contexts?status=archived").status_code == 422


def test_api_patches_context_details(client):
    created = client.post(
        "/contexts", json={"title": "Bewerbungen", "type": "topic", "knowledge_path": "bewerbungen"}
    ).json()

    patched = client.patch(
        f"/contexts/{created['id']}",
        json={"title": "Bewerbungsunterlagen", "knowledge_path": "/absolut"},
    )

    assert patched.status_code == 422
    unchanged = client.patch(f"/contexts/{created['id']}", json={}).json()
    assert without_timestamps(unchanged) == without_timestamps(created)
    assert unchanged["title"] == "Bewerbungen"


@pytest.mark.parametrize(
    "payload", [{"title": None}, {"type": None}, {"title": None, "type": None}]
)
def test_api_patch_rejects_null_for_required_context_values(client, payload):
    """Regression: PATCH mit title:null oder type:null lieferte vorher HTTP 500."""
    created = client.post("/contexts", json={"title": "Bewerbungen", "type": "topic"}).json()

    response = client.patch(f"/contexts/{created['id']}", json=payload)

    assert response.status_code == 422
    stored = client.get(f"/contexts/{created['id']}").json()
    assert stored["title"] == "Bewerbungen"
    assert stored["type"] == "topic"
    assert [
        event["event_type"] for event in client.get(f"/contexts/{created['id']}/events").json()
    ] == ["CONTEXT_CREATED"]


def test_api_patch_allows_omitting_required_context_values(client):
    """Ausgelassene Felder bleiben erlaubt; description bleibt nullable."""
    created = client.post(
        "/contexts",
        json={
            "title": "Bewerbungen",
            "type": "topic",
            "description": "Entwurf",
            "parent_context_id": None,
        },
    ).json()

    only_title = client.patch(
        f"/contexts/{created['id']}", json={"title": "Bewerbungsunterlagen"}
    ).json()

    assert only_title["title"] == "Bewerbungsunterlagen"
    assert only_title["type"] == "topic"
    assert only_title["description"] == "Entwurf"

    cleared = client.patch(f"/contexts/{created['id']}", json={"description": None}).json()
    assert cleared["description"] is None

    assert client.patch(f"/contexts/{created['id']}", json={}).status_code == 200


def test_api_patch_rejects_non_canonical_knowledge_paths(client):
    """Regression: mehrdeutige knowledge_path-Werte wurden still akzeptiert."""
    created = client.post(
        "/contexts",
        json={"title": "SAP", "type": "application", "knowledge_path": "bewerbungen/sap"},
    ).json()

    for path in ("a//b", "a/./b", "a/b/", ".", "a\x00b", "a" * 513, "/absolut", "C:/pfad", "a\\b"):
        response = client.patch(f"/contexts/{created['id']}", json={"knowledge_path": path})
        assert response.status_code == 422, path

    assert client.get(f"/contexts/{created['id']}").json()["knowledge_path"] == "bewerbungen/sap"
    assert [
        event["event_type"] for event in client.get(f"/contexts/{created['id']}/events").json()
    ] == ["CONTEXT_CREATED"]


def test_api_patch_accepts_valid_knowledge_paths(client):
    created = client.post("/contexts", json={"title": "SAP", "type": "application"}).json()

    assert (
        client.patch(
            f"/contexts/{created['id']}", json={"knowledge_path": "bewerbungen/sap"}
        ).json()["knowledge_path"]
        == "bewerbungen/sap"
    )
    # Identischer Wert bleibt ein No-op und liefert weiterhin 200.
    assert (
        client.patch(
            f"/contexts/{created['id']}", json={"knowledge_path": "bewerbungen/sap"}
        ).status_code
        == 200
    )


def test_api_patch_refuses_to_unbind_or_rebind_a_bound_knowledge_path(client):
    created = client.post("/contexts", json={"title": "SAP", "type": "application"}).json()
    client.patch(f"/contexts/{created['id']}", json={"knowledge_path": "bewerbungen/sap"})

    for payload in ({"knowledge_path": None}, {"knowledge_path": "bewerbungen/anders"}):
        response = client.patch(f"/contexts/{created['id']}", json=payload)
        assert response.status_code == 409, payload

    assert client.get(f"/contexts/{created['id']}").json()["knowledge_path"] == "bewerbungen/sap"
    assert [
        event["event_type"] for event in client.get(f"/contexts/{created['id']}/events").json()
    ] == [
        "CONTEXT_CREATED",
        "CONTEXT_UPDATED",
    ]


def test_api_refuses_a_knowledge_path_that_is_already_bound(client):
    client.post(
        "/contexts",
        json={"title": "SAP", "type": "application", "knowledge_path": "bewerbungen/sap"},
    )

    response = client.post(
        "/contexts",
        json={"title": "Finanzierung", "type": "topic", "knowledge_path": "bewerbungen/sap"},
    )

    assert response.status_code == 409
    assert [item["title"] for item in client.get("/contexts").json()] == ["SAP"]


def test_api_create_rejects_non_canonical_knowledge_paths(client):
    for path in ("a//b", "a/./b", "./a", "a/b/", "a" * 513, "a\x00b", "a\\b", "//server/share"):
        response = client.post(
            "/contexts", json={"title": "SAP", "type": "application", "knowledge_path": path}
        )
        assert response.status_code == 422, path
    assert client.get("/contexts").json() == []
