"""Topic generation contracts for actions."""

from __future__ import annotations

from support.actions import FakeProposalClient, env_setup as env_setup
from datetime import UTC, datetime
import json
from sqlalchemy import select
from organiser.db.models import (
    Inbox,
    Topic,
)
from organiser.services.context_actions import (
    apply_context_action_proposal,
    reject_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions


def test_propose_and_apply_create_topic(env_setup):
    settings, store, factory, context_id = env_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Investitionsrechnung"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-1"},
                        "confidence": 0.95,
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-1",
            text="Bitte neues Thema Investitionsrechnung anlegen",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

        proposals = propose_context_actions(
            session,
            context_id=context_id,
            store=store,
            settings=settings,
            client=client,
            source_inbox_id="inbox-1",
        )
        assert len(proposals) == 1
        prop_id = proposals[0].id
        assert proposals[0].status == "proposed"

        # Inbox is now reserved (status == processing)
        inbox_now = session.get(Inbox, "inbox-1")
        assert inbox_now.processing_status == "processing"

    # Apply the proposal
    with factory() as session:
        applied = apply_context_action_proposal(session, store, settings, prop_id)
        assert applied.status == "applied"
        assert applied.result_payload["title"] == "Investitionsrechnung"

        # Topic was created in DB
        topics = list(session.scalars(select(Topic).where(Topic.context_id == context_id)))
        assert any(t.title == "Investitionsrechnung" for t in topics)

        # Inbox is now processed (Decision A1: at least one proposal was applied)
        inbox_now = session.get(Inbox, "inbox-1")
        assert inbox_now.processing_status == "processed"
        assert inbox_now.processed_at is not None


def test_reject_all_proposals_reverts_inbox_to_pending(env_setup):
    """Decision A1: If all proposals of a generation are rejected, inbox returns to pending."""
    settings, store, factory, context_id = env_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Thema A"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-2"},
                    },
                    {
                        "action": {"action": "create_topic", "title": "Thema B"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-2"},
                    },
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-2",
            text="Zwei Themen vorschlagen",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

        proposals = propose_context_actions(
            session,
            context_id=context_id,
            store=store,
            settings=settings,
            client=client,
            source_inbox_id="inbox-2",
        )
        p1, p2 = proposals[0].id, proposals[1].id

    with factory() as session:
        # Reject first: inbox still processing because p2 is proposed
        reject_context_action_proposal(session, p1)
        inbox_now = session.get(Inbox, "inbox-2")
        assert inbox_now.processing_status == "processing"

        # Reject second: ALL proposals rejected -> inbox reverts to pending!
        reject_context_action_proposal(session, p2)
        inbox_now = session.get(Inbox, "inbox-2")
        assert inbox_now.processing_status == "pending"


def test_sibling_proposals_apply_independently_with_partial_manifests(env_setup):
    """Decision A2: Sibling proposals don't become stale when one is applied."""
    settings, store, factory, context_id = env_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Thema 1"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-3"},
                    },
                    {
                        "action": {"action": "create_task", "title": "Aufgabe 1"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-3"},
                    },
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-3",
            text="Thema und Aufgabe anlegen",
            source="test",
            created_at=datetime.now(UTC),
            received_at=datetime.now(UTC),
            processing_status="pending",
        )
        session.add(inbox)
        session.commit()

        proposals = propose_context_actions(
            session,
            context_id=context_id,
            store=store,
            settings=settings,
            client=client,
            source_inbox_id="inbox-3",
        )
        p1_id, p2_id = proposals[0].id, proposals[1].id

    with factory() as session:
        # Apply first proposal (creates topic)
        applied1 = apply_context_action_proposal(session, store, settings, p1_id)
        assert applied1.status == "applied"

        # Apply second proposal: DOES NOT FAIL with stale manifest because partial manifest only scopes relevant entities!
        applied2 = apply_context_action_proposal(session, store, settings, p2_id)
        assert applied2.status == "applied"

        inbox_now = session.get(Inbox, "inbox-3")
        assert inbox_now.processing_status == "processed"
