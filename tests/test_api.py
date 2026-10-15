import json
import sqlite3
from datetime import UTC, datetime

import pytest
from sqlalchemy import inspect, select

from conftest import AuthenticatedTestClient, make_settings
from organiser.api.app import create_app
from organiser.db.models import Inbox
from organiser.db.session import create_session_factory


class FakePlannerClient:
    model_name = "fake-planner"

    def __init__(self):
        self.response = ""

    def plan(self, _prompt: str) -> str:
        return self.response


def test_health_works(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        assert client.get("/health").json() == {"status": "ok"}


def test_inbox_is_stored_idempotently(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    payload = {"id": "iphone-1", "created_at": "2026-09-17T10:00:00+02:00", "text": "Milch kaufen"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        assert client.post("/inbox", json=payload).json() == {"status": "stored", "id": "iphone-1"}
        assert client.post("/inbox", json=payload).json() == {
            "status": "already_exists",
            "id": "iphone-1",
        }

    with create_session_factory(settings)() as session:
        assert len(session.scalars(select(Inbox).where(Inbox.id == "iphone-1")).all()) == 1


def test_inbox_file_works(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    payload = {"id": "iphone-file-1", "created_at": "2026-09-17T10:00:00Z", "text": "Dateiimport"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        response = client.post(
            "/inbox-file",
            files={"file": ("entry.json", json.dumps(payload), "application/json")},
        )
    assert response.json() == {"received": True, "id": "iphone-file-1"}


def upload_inbox_file(client, payload):
    return client.post(
        "/inbox-file", files={"file": ("entry.json", json.dumps(payload), "application/json")}
    )


def stored_created_at(settings, inbox_id: str) -> datetime:
    """Liest created_at aus der DB; SQLite liefert naiv, gespeichert wird immer UTC."""
    with create_session_factory(settings)() as session:
        stored = session.get(Inbox, inbox_id)
        return stored.created_at.replace(tzinfo=UTC)


def test_inbox_file_accepts_iso_created_at_unchanged(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    payload = {
        "id": "iphone-iso",
        "created_at": "2026-09-26T12:32:00+02:00",
        "text": "Milch kaufen",
    }
    with AuthenticatedTestClient(create_app(settings)) as client:
        assert upload_inbox_file(client, payload).json() == {"received": True, "id": "iphone-iso"}

    assert stored_created_at(settings, "iphone-iso") == datetime(2026, 9, 26, 10, 32, tzinfo=UTC)


@pytest.mark.parametrize(
    ("created_at", "expected_utc"),
    [
        ("26.09.26, 13:42", datetime(2026, 9, 26, 11, 42, tzinfo=UTC)),
        ("26.09.2026, 13:42", datetime(2026, 9, 26, 11, 42, tzinfo=UTC)),
        ("26.09.26, 13:42:07", datetime(2026, 9, 26, 11, 42, 7, tzinfo=UTC)),
        ("26.09.26, 13:42\u202f", datetime(2026, 9, 26, 11, 42, tzinfo=UTC)),
    ],
)
def test_inbox_file_accepts_shortcut_created_at(tmp_path, created_at, expected_utc):
    settings = make_settings(tmp_path, "shortcut.sqlite3", calendar_timezone="Europe/Berlin")
    payload = {"id": "iphone-shortcut", "created_at": created_at, "text": "Milch kaufen"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        assert upload_inbox_file(client, payload).json() == {
            "received": True,
            "id": "iphone-shortcut",
        }

    assert stored_created_at(settings, "iphone-shortcut") == expected_utc


def test_inbox_file_uses_the_configured_timezone_for_shortcut_created_at(tmp_path):
    settings = make_settings(tmp_path, "utc.sqlite3", calendar_timezone="UTC")
    payload = {"id": "iphone-utc", "created_at": "26.09.26, 13:42", "text": "Milch kaufen"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        assert upload_inbox_file(client, payload).status_code == 200

    assert stored_created_at(settings, "iphone-utc") == datetime(2026, 9, 26, 13, 42, tzinfo=UTC)


def test_inbox_file_reports_unusable_created_at_separately(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    payload = {"id": "iphone-bad", "created_at": "gestern", "text": "Milch kaufen"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        response = upload_inbox_file(client, payload)

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "created_at muss ein ISO-8601-Zeitstempel mit Zeitzone sein, z. B. 2026-09-26T12:32:00+02:00."
    )


def test_inbox_file_keeps_missing_fields_detail(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        response = upload_inbox_file(
            client, {"id": "iphone-missing", "created_at": "26.09.26, 13:42"}
        )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Die JSON-Datei muss die Felder id, created_at und text enthalten."
    )


def test_inbox_file_keeps_empty_field_detail(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    payload = {"id": "", "created_at": "2026-09-26T12:32:00+02:00", "text": "Milch kaufen"}
    with AuthenticatedTestClient(create_app(settings)) as client:
        response = upload_inbox_file(client, payload)

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Die JSON-Datei muss die Felder id, created_at und text enthalten."
    )


def test_inbox_file_still_rejects_non_json(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    with AuthenticatedTestClient(create_app(settings)) as client:
        response = client.post(
            "/inbox-file", files={"file": ("entry.json", b"kein json", "application/json")}
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Die hochgeladene Datei ist kein gueltiges JSON."


def test_existing_inbox_rows_are_migrated_and_readable(tmp_path):
    database_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE inbox (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, text TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO inbox (id, created_at, text) VALUES (?, ?, ?)",
            ("legacy-1", "2026-09-15T20:00:00+02:00", "Bestehender Eintrag"),
        )

    settings = make_settings(tmp_path, "legacy.sqlite3")
    with AuthenticatedTestClient(create_app(settings)):
        pass

    with create_session_factory(settings)() as session:
        item = session.get(Inbox, "legacy-1")
        assert item is not None
        assert item.text == "Bestehender Eintrag"
        assert item.source == "legacy"
        assert item.processing_status == "pending"


def test_foundation_tables_exist_after_migration(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    with AuthenticatedTestClient(create_app(settings)):
        pass

    table_names = inspect(create_session_factory(settings)().get_bind()).get_table_names()
    assert {
        "inbox",
        "projects",
        "tasks",
        "events",
        "processing_runs",
        "planner_proposals",
    }.issubset(table_names)


def test_planning_endpoints_persist_then_explicitly_apply(tmp_path):
    settings = make_settings(tmp_path, "test.sqlite3")
    fake_planner = FakePlannerClient()
    with AuthenticatedTestClient(create_app(settings, llm_client=fake_planner)) as client:
        project = client.post("/projects", json={"title": "Hausarbeit"}).json()
        fake_planner.response = json.dumps(
            {
                "target_type": "project",
                "target_id": project["id"],
                "summary": "Erster Schritt",
                "actions": [
                    {
                        "action": "create_task",
                        "title": "Gliederung erstellen",
                        "project_id": project["id"],
                        "rationale": "Schafft Struktur",
                        "missing_fields": [],
                    }
                ],
            }
        )
        proposed = client.post(f"/projects/{project['id']}/plan")
        assert proposed.status_code == 200
        assert proposed.json()["status"] == "proposed"
        assert client.get(f"/projects/{project['id']}/tasks").json() == []

        applied = client.post(f"/planning/proposals/{proposed.json()['id']}/apply")
        assert applied.status_code == 200
        assert applied.json()["status"] == "applied"
        assert (
            client.get(f"/projects/{project['id']}/tasks").json()[0]["title"]
            == "Gliederung erstellen"
        )
