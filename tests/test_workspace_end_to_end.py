"""End-to-end integration tests for Context Workspace v1 (Phases A through E).

Tests the complete lifecycle over the real FastAPI application with temporary
database, filesystem knowledge store, and isolated file sources:
1. Source setup (PDF and Markdown)
2. Context creation
3. Source registration and non-destructive scan
4. Snapshot generation (original files unmodified)
5. Context action proposals from resource snapshot (Fake LLM: CreateTopic, CreateTask)
6. Proposal review and application (Topic creation, Task creation with links)
7. Topic-Resource linking and Knowledge ensure
8. Workspace packet generation with evidence and strict budget limits (<= 32,000 chars)
9. Inbox Lernstand note processing:
   - Atomic reservation (pending -> processing)
   - Action proposals (SetTopicStatusAction + UpdateKnowledgeAction)
   - Application (DB gross status + Markdown progress percentage)
   - Final inbox resolution (processing -> processed)
10. Inbox rejection and release (no action applied -> released to pending)
11. Bit-exact immutability verification of source files
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


class FakeContextProposalClient:
    """Configurable fake LLM client for context action proposals."""

    def __init__(self, model_name: str = "fake-context-model"):
        self.model_name = model_name
        self.prompts: list[str] = []
        self._responses: list[str | dict] = []

    def set_responses(self, *responses: str | dict) -> None:
        self._responses = list(responses)

    def propose_context(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self._responses:
            return '{"proposals": []}'
        resp = self._responses.pop(0)
        if isinstance(resp, dict):
            return json.dumps(resp)
        return resp


def test_complete_context_workspace_finanzierung_lifecycle(tmp_path):
    # 1. Source Setup: PDF and Markdown in an allowed external folder
    source_root = tmp_path / "studium" / "finanzierung"
    source_root.mkdir(parents=True)
    pdf_bytes = make_pdf_bytes(
        [
            "Barwert und Kapitalwertmethode. Investitionsrechnung Grundlagen.",
            "Zinseszins und Annuitatendarlehen.",
        ]
    )
    (source_root / "Skript_Finanzierung.pdf").write_bytes(pdf_bytes)
    (source_root / "Vorlesung_01.md").write_text(
        "# Vorlesung 1: Einfuehrung in die Investitionsrechnung\nUeberblick und Grundlagen.",
        encoding="utf-8",
    )
    hashes_before = _tree_hashes(source_root)

    knowledge_root = tmp_path / "knowledge"
    settings = make_settings(
        tmp_path,
        "finanzierung_e2e.sqlite3",
        knowledge_root=knowledge_root,
        source_allowed_roots=(tmp_path.resolve(),),
    )
    headers = auth_headers()
    fake_client = FakeContextProposalClient()

    with TestClient(create_app(settings, llm_client=fake_client)) as client:
        # 2. Context creation
        ctx_resp = client.post(
            "/contexts",
            json={"title": "Finanzierung", "type": "course"},
            headers=headers,
        )
        assert ctx_resp.status_code == 201
        context_id = ctx_resp.json()["id"]

        # 3. Source registration
        src_resp = client.post(
            f"/contexts/{context_id}/sources",
            json={"source_type": "folder", "path": str(source_root)},
            headers=headers,
        )
        assert src_resp.status_code == 201
        source_id = src_resp.json()["id"]

        # 4. Non-destructive Scan
        scan_resp = client.post(
            f"/contexts/{context_id}/sources/{source_id}/scan",
            headers=headers,
        )
        assert scan_resp.status_code == 200
        scan_data = scan_resp.json()
        assert scan_data["status"] == "completed"
        assert scan_data["complete"] is True
        assert scan_data["counters"]["created"] == 2

        # Verify sources/resources
        res_list = client.get(f"/contexts/{context_id}/resources", headers=headers).json()
        assert res_list["total"] == 2
        by_name = {r["relative_path"]: r for r in res_list["items"]}
        assert "Skript_Finanzierung.pdf" in by_name
        assert "Vorlesung_01.md" in by_name

        pdf_resource = by_name["Skript_Finanzierung.pdf"]
        pdf_res_id = pdf_resource["id"]
        pdf_detail = client.get(f"/resources/{pdf_res_id}", headers=headers).json()
        snapshot = pdf_detail["current_snapshot"]
        assert snapshot is not None
        assert "Barwert" in snapshot["text"]
        snapshot_id = snapshot["id"]
        snapshot_hash = snapshot["content_hash"]

        # Original source files must never contain leaks of local path in API response
        assert str(source_root) not in json.dumps(res_list)
        assert str(source_root) not in json.dumps(pdf_detail)

        # Scan never creates topics or tasks automatically
        assert client.get(f"/contexts/{context_id}/topics", headers=headers).json() == []
        assert client.get(f"/tasks?context_id={context_id}", headers=headers).json() == []

        # 5. Propose actions from Snapshot (Create Topic + Create Task)
        fake_client.set_responses(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "create_topic",
                            "title": "Investitionsrechnung",
                        },
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snapshot_id,
                            "content_hash": snapshot_hash,
                        },
                        "confidence": 0.95,
                    },
                    {
                        "action": {
                            "action": "create_task",
                            "title": "Uebungsblatt 1: Barwert berechnen",
                            "description": "Aufgaben 1-4 zur Kapitalwertmethode",
                            "resource_ids": [pdf_res_id],
                            "estimated_minutes": 90,
                        },
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snapshot_id,
                            "content_hash": snapshot_hash,
                        },
                        "confidence": 0.90,
                    },
                ]
            }
        )

        propose_resp = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"resource_snapshot_id": snapshot_id},
            headers=headers,
        )
        assert propose_resp.status_code == 201, propose_resp.text
        proposals = propose_resp.json()
        assert len(proposals) == 2
        topic_prop = next(p for p in proposals if p["action_payload"]["action"] == "create_topic")
        task_prop = next(p for p in proposals if p["action_payload"]["action"] == "create_task")
        assert topic_prop["status"] == "proposed"
        assert task_prop["status"] == "proposed"

        # 6. Apply Topic Proposal
        apply_topic_resp = client.post(
            f"/context-action-proposals/{topic_prop['id']}/apply",
            headers=headers,
        )
        assert apply_topic_resp.status_code == 200, apply_topic_resp.text
        assert apply_topic_resp.json()["status"] == "applied"

        topics = client.get(f"/contexts/{context_id}/topics", headers=headers).json()
        assert len(topics) == 1
        assert topics[0]["title"] == "Investitionsrechnung"
        assert topics[0]["status"] == "not_started"
        topic_id = topics[0]["id"]

        # 7. Apply Task Proposal (links task to context & resource)
        apply_task_resp = client.post(
            f"/context-action-proposals/{task_prop['id']}/apply",
            headers=headers,
        )
        assert apply_task_resp.status_code == 200, apply_task_resp.text
        assert apply_task_resp.json()["status"] == "applied"

        # Link Resource to Topic
        link_resp = client.post(f"/topics/{topic_id}/resources/{pdf_res_id}", headers=headers)
        assert link_resp.status_code == 201

        tasks = client.get(f"/tasks?context_id={context_id}", headers=headers).json()
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Uebungsblatt 1: Barwert berechnen"
        assert tasks[0]["context_id"] == context_id
        task_id = tasks[0]["id"]

        # Verify task resource link
        task_resources = client.get(f"/tasks/{task_id}/resources", headers=headers).json()
        assert any(r["id"] == pdf_res_id for r in task_resources)

        # 8. Ensure Knowledge for Context and Topic
        assert (
            client.post(f"/contexts/{context_id}/knowledge/ensure", headers=headers).status_code
            == 200
        )
        assert (
            client.post(f"/topics/{topic_id}/knowledge/ensure", headers=headers).status_code == 200
        )

        # 9. GET Workspace Packet & Evidence verification
        ws_resp = client.get(f"/contexts/{context_id}/workspace", headers=headers)
        assert ws_resp.status_code == 200
        ws_data = ws_resp.json()
        assert len(ws_resp.text) <= MAX_PACKET_CHARS
        assert str(source_root) not in ws_resp.text
        assert str(tmp_path) not in ws_resp.text
        assert any(t["id"] == topic_id for t in ws_data["topics"])
        assert any(t["id"] == task_id for t in ws_data["current_tasks"])
        assert any(r["id"] == pdf_res_id for r in ws_data["resources"])

        # 10. Inbox Lernstand note processing
        inbox_add_resp = client.post(
            "/inbox",
            json={
                "id": "inbox-lernstand-01",
                "created_at": "2026-10-14T10:00:00Z",
                "text": "Lernstand Investitionsrechnung: Kapitel 1 Barwert zu 30% abgeschlossen.",
            },
            headers=headers,
        )
        assert inbox_add_resp.status_code == 200
        inbox_id = "inbox-lernstand-01"

        # Model proposes setting topic status to in_progress and updating qualitative knowledge
        fake_client.set_responses(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "set_topic_status",
                            "topic_id": topic_id,
                            "status": "in_progress",
                        },
                        "evidence": {
                            "kind": "inbox",
                            "source_id": inbox_id,
                        },
                        "confidence": 0.95,
                    },
                    {
                        "action": {
                            "action": "update_knowledge",
                            "target": "topic",
                            "topic_id": topic_id,
                            "percent": 30,
                            "text": "Kapitel 1 Barwert abgeschlossen (30% verstanden).",
                        },
                        "evidence": {
                            "kind": "inbox",
                            "source_id": inbox_id,
                        },
                        "confidence": 0.90,
                    },
                ]
            }
        )

        inbox_prop_resp = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": inbox_id},
            headers=headers,
        )
        assert inbox_prop_resp.status_code == 201, inbox_prop_resp.text
        inbox_proposals = inbox_prop_resp.json()
        assert len(inbox_proposals) == 2

        # Second concurrent attempt to propose for the same inbox item fails closed
        dup_attempt = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": inbox_id},
            headers=headers,
        )
        assert dup_attempt.status_code == 409

        status_prop = next(
            p for p in inbox_proposals if p["action_payload"]["action"] == "set_topic_status"
        )
        know_prop = next(
            p for p in inbox_proposals if p["action_payload"]["action"] == "update_knowledge"
        )

        # Apply knowledge proposal first (updates Markdown file without mutating Topic DB entity)
        app_know_resp = client.post(
            f"/context-action-proposals/{know_prop['id']}/apply",
            headers=headers,
        )
        assert app_know_resp.status_code == 200, app_know_resp.text
        know_result = app_know_resp.json()["result_payload"]
        assert know_result["changed"] is True
        rel_path = know_result["relative_path"]

        # Apply status proposal -> DB gross status updated
        app_status_resp = client.post(
            f"/context-action-proposals/{status_prop['id']}/apply",
            headers=headers,
        )
        assert app_status_resp.status_code == 200, app_status_resp.text
        topic_updated = client.get(f"/topics/{topic_id}", headers=headers).json()
        assert topic_updated["status"] == "in_progress"

        # Verify Markdown file content on disk
        topic_md_file = next(knowledge_root.rglob(f"*{rel_path}"))
        markdown_content = topic_md_file.read_text(encoding="utf-8")
        assert "Fortschritt: 30 %" in markdown_content
        assert "Kapitel 1 Barwert abgeschlossen" in markdown_content

        # All proposals for this inbox item are applied -> Inbox item is marked processed
        # Re-proposing now fails with 409 already processed
        already_done = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": inbox_id},
            headers=headers,
        )
        assert already_done.status_code == 409
        assert "bereits verarbeitet" in already_done.text

        # 11. Rejection test: inbox item released to pending when all proposals rejected
        client.post(
            "/inbox",
            json={
                "id": "inbox-unclear-02",
                "created_at": "2026-10-14T11:00:00Z",
                "text": "Vielleicht noch ein Thema anlegen?",
            },
            headers=headers,
        )
        fake_client.set_responses(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "create_topic",
                            "title": "Unklares Thema",
                        },
                        "evidence": {
                            "kind": "inbox",
                            "source_id": "inbox-unclear-02",
                        },
                        "confidence": 0.5,
                    }
                ]
            }
        )
        unclear_props = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": "inbox-unclear-02"},
            headers=headers,
        ).json()
        reject_resp = client.post(
            f"/context-action-proposals/{unclear_props[0]['id']}/reject",
            headers=headers,
        )
        assert reject_resp.status_code == 200
        assert reject_resp.json()["status"] == "rejected"

        # Since it was rejected and released to pending, proposing again succeeds
        fake_client.set_responses({"proposals": []})
        retry_resp = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": "inbox-unclear-02"},
            headers=headers,
        )
        assert retry_resp.status_code == 201

    # 12. Immutability verification: Original source files were never modified
    assert _tree_hashes(source_root) == hashes_before, (
        "Scan/proposals must never alter source files"
    )


def test_intervening_entity_mutation_prevents_stale_apply(tmp_path):
    """Verify that an intervening modification of an entity referenced in a proposal's

    partial manifest causes the apply step to fail closed with 409 Conflict.
    """
    source_root = tmp_path / "studium"
    source_root.mkdir(parents=True)
    (source_root / "Skript.pdf").write_bytes(make_pdf_bytes(["Finanzierungsarten"]))

    knowledge_root = tmp_path / "knowledge"
    settings = make_settings(
        tmp_path,
        "stale_test.sqlite3",
        knowledge_root=knowledge_root,
        source_allowed_roots=(tmp_path.resolve(),),
    )
    headers = auth_headers()
    fake_client = FakeContextProposalClient()

    with TestClient(create_app(settings, llm_client=fake_client)) as client:
        ctx = client.post(
            "/contexts", json={"title": "Finanzierung", "type": "course"}, headers=headers
        ).json()
        context_id = ctx["id"]

        src = client.post(
            f"/contexts/{context_id}/sources",
            json={"source_type": "folder", "path": str(source_root)},
            headers=headers,
        ).json()
        client.post(f"/contexts/{context_id}/sources/{src['id']}/scan", headers=headers)

        res = client.get(f"/contexts/{context_id}/resources", headers=headers).json()["items"][0]
        res_id = res["id"]
        detail = client.get(f"/resources/{res_id}", headers=headers).json()
        snapshot_id = detail["current_snapshot"]["id"]
        snapshot_hash = detail["current_snapshot"]["content_hash"]

        # Propose a topic and a task referencing the resource
        fake_client.set_responses(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Eigenkapital"},
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snapshot_id,
                            "content_hash": snapshot_hash,
                        },
                        "confidence": 0.9,
                    },
                    {
                        "action": {
                            "action": "create_task",
                            "title": "Aufgabe zu Eigenkapital",
                            "resource_ids": [res_id],
                        },
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snapshot_id,
                            "content_hash": snapshot_hash,
                        },
                        "confidence": 0.9,
                    },
                ]
            }
        )

        proposals = client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"resource_snapshot_id": snapshot_id},
            headers=headers,
        ).json()
        topic_prop = next(p for p in proposals if p["action_payload"]["action"] == "create_topic")
        task_prop = next(p for p in proposals if p["action_payload"]["action"] == "create_task")

        # Apply topic
        client.post(f"/context-action-proposals/{topic_prop['id']}/apply", headers=headers)
        topic_id = client.get(f"/contexts/{context_id}/topics", headers=headers).json()[0]["id"]

        # Intervening write: externally link the resource to the topic, mutating resource.topic_ids and updated_at
        link_resp = client.post(f"/topics/{topic_id}/resources/{res_id}", headers=headers)
        assert link_resp.status_code == 201

        # Now attempting to apply task_prop fails closed because the resource is stale!
        apply_stale = client.post(
            f"/context-action-proposals/{task_prop['id']}/apply", headers=headers
        )
        assert apply_stale.status_code == 409
        assert "stale" in apply_stale.text.lower()
