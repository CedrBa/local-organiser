"""Concurrency, race-condition and fault-injection tests for context action proposals."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import threading

import pytest
from sqlalchemy import select

from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import ContextActionProposal, Inbox, Topic
from organiser.db.session import create_session_factory
from organiser.domain.proposals import InterpretationProposal
from organiser.services.context_actions import (
    apply_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.context_knowledge import (
    ContextKnowledgeService,
    shared_knowledge_store,
)
from organiser.services.domain import ConflictError, create_context
from organiser.services.interpreter import interpret_inbox
from organiser.services.triage import apply_manual_proposal
from support.actions import FakeProposalClient


@pytest.fixture
def race_setup(tmp_path):
    settings = make_settings(
        tmp_path,
        "races.sqlite3",
        knowledge_root=tmp_path / "knowledge",
        source_allowed_roots=(tmp_path.resolve(),),
    )
    upgrade_database(settings)
    store = shared_knowledge_store(settings)
    factory = create_session_factory(settings)

    with factory() as session:
        ctx = create_context(session, title="Kurs Concurrency", type="course")
        context_id = ctx.id
        session.commit()

    return settings, store, factory, context_id


def test_concurrent_apply_on_same_proposal_only_one_succeeds(race_setup):
    settings, store, factory, context_id = race_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Parallel Topic"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-race-1"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-race-1",
            text="Paralleles Thema",
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
            source_inbox_id="inbox-race-1",
        )
        prop_id = proposals[0].id

    results = []
    errors = []

    def run_apply():
        with factory() as sess:
            try:
                res = apply_context_action_proposal(sess, store, settings, prop_id)
                results.append(res)
            except ConflictError as e:
                errors.append(e)

    t1 = threading.Thread(target=run_apply)
    t2 = threading.Thread(target=run_apply)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one succeeded and one got ConflictError!
    assert len(results) == 1
    assert len(errors) == 1
    assert "nicht mehr im Status 'proposed'" in str(errors[0]) or "Status" in str(errors[0])

    # Exactly one topic created
    with factory() as session:
        topics = list(session.scalars(select(Topic).where(Topic.context_id == context_id)))
        assert len(topics) == 1
        assert topics[0].title == "Parallel Topic"


def test_legacy_interpreter_and_manual_triage_blocked_by_v2_reservation(race_setup):
    settings, store, factory, context_id = race_setup

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Thema V2"},
                        "evidence": {"kind": "inbox", "source_id": "inbox-lock-1"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-lock-1",
            text="Thema sperren",
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
            source_inbox_id="inbox-lock-1",
        )
        assert len(proposals) == 1

    # 1. Legacy /interpret call must be refused with ConflictError
    class DummyLegacyClient:
        model_name = "dummy"

        def interpret(self, prompt: str) -> str:
            return "{}"

    with factory() as session:
        with pytest.raises(ConflictError, match="wird bereits verarbeitet"):
            interpret_inbox(session, inbox_id="inbox-lock-1", client=DummyLegacyClient())

    # 2. Legacy manual triage call must be refused with ConflictError
    proposal = InterpretationProposal(
        source_inbox_id="inbox-lock-1",
        action="create_task",
        title="Unerlaubter Task",
    )
    with factory() as session:
        with pytest.raises(ConflictError, match="wird bereits verarbeitet"):
            apply_manual_proposal(session, proposal)

    # 3. Second V2 generation must also be refused
    with factory() as session:
        with pytest.raises(ConflictError, match="wird bereits verarbeitet"):
            propose_context_actions(
                session,
                context_id=context_id,
                store=store,
                settings=settings,
                client=client,
                source_inbox_id="inbox-lock-1",
            )


def test_fault_injection_knowledge_write_ambiguous_state_and_no_retry(race_setup, monkeypatch):
    """Decision A4: Ambiguous outcome keeps inbox processing, prevents auto-retry."""
    settings, store, factory, context_id = race_setup

    with factory() as session:
        ContextKnowledgeService(session, store).ensure_context_knowledge(context_id)
        session.commit()

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "update_knowledge",
                            "target": "progress",
                            "text": "Versuchter Fortschritt",
                        },
                        "evidence": {"kind": "inbox", "source_id": "inbox-fault"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-fault",
            text="Fehlerhafter Schreibversuch",
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
            source_inbox_id="inbox-fault",
        )
        p_id = proposals[0].id

    # Inject failure into store.apply
    def broken_apply(*args, **kwargs):
        raise OSError("Disk failure during write")

    monkeypatch.setattr(store, "apply", broken_apply)

    with factory() as session:
        with pytest.raises(ConflictError, match="Knowledge-Schreibvorgang"):
            apply_context_action_proposal(session, store, settings, p_id)

    # Check status of proposal and inbox
    with factory() as session:
        prop = session.get(ContextActionProposal, p_id)
        assert prop.status == "ambiguous"
        assert prop.error_code in ("knowledge_ambiguous", "write_failure")

        inbox_now = session.get(Inbox, "inbox-fault")
        # Decision A4: Inbox stays in 'processing' (not marked processed, not reset to pending)
        assert inbox_now.processing_status == "processing"

    # Repeated apply call must be refused! No automatic retry!
    with factory() as session:
        with pytest.raises(
            ConflictError, match="Status 'ambiguous' und kann nicht angewendet werden"
        ):
            apply_context_action_proposal(session, store, settings, p_id)
