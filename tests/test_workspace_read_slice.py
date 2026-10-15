"""Read-Slice-Integration (B + C + D) ueber die echte App.

Nur temporaere DB, temporaeres Knowledge-Root und temporaere Quelldateien; kein
Modell, kein Netz. Prueft die Verdrahtung in ``create_app``: Source-Policy, Scan,
Topic, Task-Links, Knowledge-Ensure, GET workspace, Task-Context und die
Task-Filter -- und dass der Scan Originale nie veraendert.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from conftest import auth_headers, make_settings
from organiser.api.app import create_app
from support.ingestion import make_pdf_bytes

MAX_PACKET_CHARS = 32000


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_read_slice_end_to_end(tmp_path):
    source_root = tmp_path / "studium" / "finanzierung"
    source_root.mkdir(parents=True)
    (source_root / "Skript.pdf").write_bytes(
        make_pdf_bytes(["Barwert und Annuitaet", "Zins und Tilgung"])
    )
    (source_root / "Notiz.md").write_text("# Bilanzierung\nAktiva und Passiva", encoding="utf-8")
    before = _tree_hashes(source_root)

    settings = make_settings(
        tmp_path,
        "e2e.sqlite3",
        knowledge_root=tmp_path / "knowledge",
        source_allowed_roots=(tmp_path.resolve(),),
    )
    headers = auth_headers()
    with TestClient(create_app(settings)) as client:
        context = client.post(
            "/contexts", json={"title": "Finanzierung", "type": "course"}, headers=headers
        )
        assert context.status_code == 201
        context_id = context.json()["id"]

        source = client.post(
            f"/contexts/{context_id}/sources",
            json={"source_type": "folder", "path": str(source_root)},
            headers=headers,
        )
        assert source.status_code == 201
        source_id = source.json()["id"]

        scan = client.post(f"/contexts/{context_id}/sources/{source_id}/scan", headers=headers)
        assert scan.status_code == 200, scan.text
        assert scan.json()["status"] == "completed" and scan.json()["complete"] is True
        assert scan.json()["counters"]["created"] == 2

        resources = client.get(f"/contexts/{context_id}/resources", headers=headers).json()
        assert resources["total"] == 2
        by_path = {item["relative_path"]: item["id"] for item in resources["items"]}
        assert set(by_path) == {"Skript.pdf", "Notiz.md"}
        assert str(source_root) not in json.dumps(resources)

        detail = client.get(f"/resources/{by_path['Skript.pdf']}", headers=headers).json()
        assert "Barwert" in detail["current_snapshot"]["text"]
        assert str(source_root) not in json.dumps(detail)

        # Scan darf weder Tasks noch Topics anlegen.
        assert client.get("/tasks", headers=headers).json() == []
        assert client.get(f"/contexts/{context_id}/topics", headers=headers).json() == []

        topic = client.post(
            f"/contexts/{context_id}/topics", json={"title": "Bilanzierung"}, headers=headers
        )
        assert topic.status_code == 201
        topic_id = topic.json()["id"]
        link = client.post(f"/topics/{topic_id}/resources/{by_path['Notiz.md']}", headers=headers)
        assert link.status_code == 201

        # Task erbt Kontext nicht aus dem Nichts; explizit gesetzt und per Filter auffindbar.
        task = client.post(
            "/tasks",
            json={"title": "Uebung 3", "context_id": context_id, "topic_id": topic_id},
            headers=headers,
        )
        assert task.status_code == 201, task.text
        task_id = task.json()["id"]
        assert task.json()["context_id"] == context_id and task.json()["topic_id"] == topic_id
        assert (
            client.post(
                f"/tasks/{task_id}/resources/{by_path['Skript.pdf']}", headers=headers
            ).status_code
            == 201
        )

        # missing vs. null: Kind erbt vom Parent, explizites null nicht.
        inherited = client.post(
            "/tasks", json={"title": "Teilaufgabe", "parent_task_id": task_id}, headers=headers
        )
        assert inherited.status_code == 201, inherited.text
        explicit_null = client.post(
            "/tasks",
            json={
                "title": "Ohne Kontext",
                "parent_task_id": task_id,
                "context_id": None,
                "topic_id": None,
            },
            headers=headers,
        )
        assert explicit_null.status_code == 201, explicit_null.text
        assert inherited.json()["context_id"] == context_id
        assert explicit_null.json()["context_id"] is None

        filtered = client.get(
            f"/tasks?context_id={context_id}&topic_id={topic_id}", headers=headers
        ).json()
        assert task_id in {item["id"] for item in filtered}
        assert explicit_null.json()["id"] not in {
            item["id"]
            for item in client.get(f"/tasks?context_id={context_id}", headers=headers).json()
        }

        ensure = client.post(f"/contexts/{context_id}/knowledge/ensure", headers=headers)
        assert ensure.status_code == 200, ensure.text
        assert (
            client.post(f"/topics/{topic_id}/knowledge/ensure", headers=headers).status_code == 200
        )

        workspace = client.get(f"/contexts/{context_id}/workspace", headers=headers)
        assert workspace.status_code == 200, workspace.text
        assert len(workspace.text) <= MAX_PACKET_CHARS
        assert str(source_root) not in workspace.text and str(tmp_path) not in workspace.text
        packet = workspace.json()
        assert "Bilanzierung" in workspace.text and "Skript.pdf" in workspace.text

        # Deterministisch und ohne Schreibzugriff.
        again = client.get(f"/contexts/{context_id}/workspace", headers=headers)
        assert again.json() == packet

        # Fremde/unbekannte Fokus-IDs werden abgelehnt.
        assert (
            client.get(
                f"/contexts/{context_id}/workspace?topic_id=unbekannt", headers=headers
            ).status_code
            == 422
        )

        task_ctx = client.get(f"/tasks/{task_id}/context", headers=headers)
        assert task_ctx.status_code == 200, task_ctx.text
        assert task_ctx.json()["workspace"] is not None
        assert str(tmp_path) not in task_ctx.text
        # Task ohne Kontext bekommt kein Workspace.
        no_ctx = client.get(f"/tasks/{explicit_null.json()['id']}/context", headers=headers)
        assert no_ctx.json()["workspace"] is None

    assert _tree_hashes(source_root) == before, "Scan/Workspace duerfen Originale nicht veraendern"
