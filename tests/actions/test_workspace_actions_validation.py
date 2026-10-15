"""Validation contracts for actions."""

from __future__ import annotations

from support.actions import FakeProposalClient, env_setup as env_setup
from datetime import UTC, datetime
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from organiser.api.context_actions import build_router
from organiser.db.models import (
    Inbox,
)
from organiser.domain.context_actions import (
    MAX_PROPOSAL_OUTPUT_CHARS,
    ProposalValidationError,
)
from organiser.services.context_interpreter import propose_context_actions


def test_invented_ids_in_model_output_are_rejected(env_setup):
    settings, store, factory, context_id = env_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "set_topic_status",
                            "topic_id": "erfundene-id",
                            "status": "done",
                        },
                        "evidence": {"kind": "inbox", "source_id": "inbox-inv"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-inv",
            text="Unbekanntes Thema beenden",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

        with pytest.raises(ProposalValidationError, match="Ungueltige topic_id"):
            propose_context_actions(
                session,
                context_id=context_id,
                store=store,
                settings=settings,
                client=client,
                source_inbox_id="inbox-inv",
            )

        # On validation failure, inbox reservation is released back to pending
        inbox_now = session.get(Inbox, "inbox-inv")
        assert inbox_now.processing_status == "pending"


def test_output_character_limit_is_fail_closed(env_setup):
    settings, store, factory, context_id = env_setup

    long_output = "x" * (MAX_PROPOSAL_OUTPUT_CHARS + 10)
    client = FakeProposalClient(output_generator=long_output)

    with factory() as session:
        inbox = Inbox(
            id="inbox-long",
            text="Sehr lange Antwort erwartet",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

        with pytest.raises(ProposalValidationError, match="ueberschreitet Limit"):
            propose_context_actions(
                session,
                context_id=context_id,
                store=store,
                settings=settings,
                client=client,
                source_inbox_id="inbox-long",
            )

        inbox_now = session.get(Inbox, "inbox-long")
        assert inbox_now.processing_status == "pending"


def test_api_endpoints_work_end_to_end(env_setup):
    settings, store, factory, context_id = env_setup

    def get_session():
        with factory() as session:
            yield session

    def get_store():
        return store

    client_fake = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "API Thema"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-api"},
                    }
                ]
            }
        )
    )

    app = FastAPI()
    router = build_router(get_session, get_store, settings, client=client_fake)
    app.include_router(router)

    with factory() as session:
        inbox = Inbox(
            id="inbox-api",
            text="Thema per API",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

    with TestClient(app) as http_client:
        # POST generate
        resp = http_client.post(
            f"/contexts/{context_id}/action-proposals",
            json={"source_inbox_id": "inbox-api"},
        )
        assert resp.status_code == 201, resp.text
        data = resp.json()
        assert len(data) == 1
        prop_id = data[0]["id"]
        assert data[0]["status"] == "proposed"

        # GET proposal
        get_resp = http_client.get(f"/context-action-proposals/{prop_id}")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == prop_id

        # GET list
        list_resp = http_client.get(f"/contexts/{context_id}/action-proposals")
        assert list_resp.status_code == 200
        assert len(list_resp.json()) == 1

        # POST apply
        apply_resp = http_client.post(f"/context-action-proposals/{prop_id}/apply")
        assert apply_resp.status_code == 200
        assert apply_resp.json()["status"] == "applied"
