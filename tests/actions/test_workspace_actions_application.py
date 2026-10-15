"""Application contracts for actions."""

from __future__ import annotations

from support.actions import FakeProposalClient, env_setup as env_setup
from datetime import UTC, datetime
import json
import pytest
from organiser.db.models import (
    Context,
    Inbox,
    Topic,
)
from organiser.services.context_actions import (
    apply_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.context_knowledge import (
    ContextKnowledgeService,
)
from organiser.services.domain import (
    ConflictError,
)
from organiser.services.topics import create_topic


def test_stale_manifest_rejects_apply_when_topic_modified(env_setup):
    settings, store, factory, context_id = env_setup

    with factory() as session:
        topic = create_topic(session, context_id=context_id, title="Altes Thema")
        topic_id = topic.id
        session.commit()

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "set_topic_status",
                            "topic_id": topic_id,
                            "status": "in_progress",
                        },
                        "evidence": {"kind": "inbox", "source_id": "inbox-stale"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-stale",
            text="Thema starten",
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
            source_inbox_id="inbox-stale",
        )
        p_id = proposals[0].id

    # In the meantime, someone changes the topic title!
    with factory() as session:
        t = session.get(Topic, topic_id)
        t.title = "Geaendertes Thema"
        session.commit()

    with factory() as session:
        with pytest.raises(ConflictError, match="stale"):
            apply_context_action_proposal(session, store, settings, p_id)


def test_update_knowledge_applies_qualitative_progress(env_setup):
    settings, store, factory, context_id = env_setup

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
                            "text": "Kapitel 1 vollstaendig gelesen.",
                            "percent": 30,
                        },
                        "evidence": {"kind": "inbox", "source_id": "inbox-prog"},
                    }
                ]
            }
        )
    )

    with factory() as session:
        inbox = Inbox(
            id="inbox-prog",
            text="Fortschritt notieren",
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
            source_inbox_id="inbox-prog",
        )
        p_id = proposals[0].id

    with factory() as session:
        applied = apply_context_action_proposal(session, store, settings, p_id)
        assert applied.status == "applied"

    # Verify markdown file
    with factory() as session:
        ctx = session.get(Context, context_id)
        content = (store.root / ctx.knowledge_path / "progress.md").read_text(encoding="utf-8")
        assert "Kapitel 1 vollstaendig gelesen." in content
        assert "30 %" in content
