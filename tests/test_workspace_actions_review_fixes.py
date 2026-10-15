"""Regression gates for the independent Context Workspace merge review."""

from datetime import UTC, datetime
import json
import threading

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, text

from conftest import auth_headers
from organiser.api.app import create_app
from organiser.db.models import (
    ContextActionProposal,
    Event,
    Inbox,
    ProcessingRun,
    Resource,
    ResourceSnapshot,
    Topic,
)
from organiser.domain.context_actions import ProposalValidationError, knowledge_target_path
from organiser.services import context_actions, context_interpreter
from organiser.services.context_actions import (
    apply_context_action_proposal,
    reject_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.context_knowledge import ContextKnowledgeService
from organiser.services.domain import (
    ConflictError,
    create_task,
    update_context,
    update_task_details,
)
from organiser.services.knowledge import MAX_EXCERPT_BYTES
from organiser.services.topic_knowledge import TopicKnowledgeService
from organiser.services.topics import create_topic, set_topic_status
from organiser.services.workspace_context import WorkspacePolicy
from support.actions import FakeProposalClient, env_setup as env_setup


def _generate(env, actions, inbox_id="review-inbox", output=None):
    settings, store, factory, cid = env
    with factory() as session:
        session.add(
            Inbox(
                id=inbox_id,
                text="Review input",
                source="test",
                processing_status="pending",
                created_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
            )
        )
        session.commit()
        client = FakeProposalClient(
            output_generator=json.dumps(
                output
                if output is not None
                else {
                    "proposals": [
                        {"action": action, "evidence": {"kind": "inbox", "source_id": inbox_id}}
                        for action in actions
                    ]
                }
            )
        )
        return [
            p.id
            for p in propose_context_actions(
                session,
                context_id=cid,
                store=store,
                settings=settings,
                client=client,
                source_inbox_id=inbox_id,
            )
        ]


def _knowledge(env, target):
    _, store, factory, cid = env
    with factory() as session:
        update_context(session, context_id=cid, knowledge_path="courses/finance")
        ContextKnowledgeService(session, store).ensure_context_knowledge(cid)
        topic_id = None
        if target == "topic":
            topic_id = create_topic(session, context_id=cid, title="Topic knowledge").id
            TopicKnowledgeService(session, store).ensure_topic_knowledge(topic_id)
    action = {"action": "update_knowledge", "target": target, "text": "Approved review note"}
    if topic_id:
        action["topic_id"] = topic_id
    return action, store.root / knowledge_target_path("courses/finance", target, topic_id)


@pytest.mark.parametrize("target", ["overview", "progress", "note", "topic"])
@pytest.mark.parametrize("omitted", [False, True])
@pytest.mark.parametrize("edited", [False, True])
def test_knowledge_destination_version_including_budget_omissions(
    env_setup, monkeypatch, target, omitted, edited
):
    action, path = _knowledge(env_setup, target)
    if omitted:
        original = context_interpreter.workspace_context
        monkeypatch.setattr(
            context_interpreter,
            "workspace_context",
            lambda *args, **kwargs: original(
                *args, **kwargs, policy=WorkspacePolicy(max_excerpts=0)
            ),
        )
    [pid] = _generate(env_setup, [action])
    settings, store, factory, cid = env_setup
    with factory() as session:
        p = session.get(ContextActionProposal, pid)
        hashes = p.preconditions["manifest"]["knowledge"]
        assert len(hashes) == 1
        assert hashes[0]["relative_path"] == path.relative_to(store.root).as_posix()
        assert hashes[0]["context_id"] == cid
        if omitted:
            assert p.packet_payload["manifest"]["knowledge"] == []
    if edited:
        path.write_text(path.read_text() + "\nManual edit after approval\n")
    before = path.read_bytes()
    with factory() as session:
        if edited:
            with pytest.raises(ConflictError, match="stale"):
                apply_context_action_proposal(session, store, settings, pid)
            assert path.read_bytes() == before
            assert session.get(ContextActionProposal, pid).status == "proposed"
        else:
            assert apply_context_action_proposal(session, store, settings, pid).status == "applied"
            assert "Approved review note" in path.read_text()


@pytest.mark.parametrize("target", ["overview", "progress", "note", "topic"])
def test_missing_knowledge_manifest_entry_cannot_pass_apply(env_setup, target):
    action, path = _knowledge(env_setup, target)
    [pid] = _generate(env_setup, [action])
    settings, store, factory, _ = env_setup
    with factory() as session:
        p = session.get(ContextActionProposal, pid)
        p.preconditions = {
            **p.preconditions,
            "manifest": {**p.preconditions["manifest"], "knowledge": []},
        }
        session.commit()
    before = path.read_bytes()
    with factory() as session:
        with pytest.raises(ConflictError, match="Dateiversion fehlt"):
            apply_context_action_proposal(session, store, settings, pid)
    assert path.read_bytes() == before


@pytest.mark.parametrize("defect", ["missing", "oversized"])
def test_unreadable_omitted_destination_refuses_generation(env_setup, monkeypatch, defect):
    action, path = _knowledge(env_setup, "note")
    original = context_interpreter.workspace_context
    monkeypatch.setattr(
        context_interpreter,
        "workspace_context",
        lambda *args, **kwargs: original(*args, **kwargs, policy=WorkspacePolicy(max_excerpts=0)),
    )
    if defect == "missing":
        path.unlink()
    else:
        path.write_bytes(b"x" * (MAX_EXCERPT_BYTES + 1))
    with pytest.raises(ProposalValidationError, match="begrenzt lesbar"):
        _generate(env_setup, [action])
    with env_setup[2]() as session:
        assert session.get(Inbox, "review-inbox").processing_status == "pending"
        assert not list(session.scalars(select(ContextActionProposal)))


@pytest.mark.parametrize("task_kind", ["unknown", "unbound", "omitted"])
def test_link_task_rejects_ids_not_delivered_in_packet(env_setup, task_kind):
    _, _, factory, cid = env_setup
    with factory() as session:
        if task_kind == "unknown":
            tid = "invented-task"
        elif task_kind == "unbound":
            tid = create_task(session, title="Outside workspace").id
        else:
            for index in range(21):
                tid = create_task(session, title=f"Task {index}", context_id=cid).id
    with pytest.raises(ProposalValidationError, match="task_id"):
        _generate(env_setup, [{"action": "link_task_context", "task_id": tid}])
    with factory() as session:
        assert not list(session.scalars(select(ContextActionProposal)))
        assert session.get(Inbox, "review-inbox").processing_status == "pending"


def test_delivered_task_has_manifest_and_detects_later_change(env_setup):
    settings, store, factory, cid = env_setup
    with factory() as session:
        tid = create_task(session, title="Delivered task", context_id=cid).id
    [pid] = _generate(env_setup, [{"action": "link_task_context", "task_id": tid}])
    with factory() as session:
        p = session.get(ContextActionProposal, pid)
        assert any(
            e["entity_type"] == "task" and e["entity_id"] == tid
            for e in p.preconditions["manifest"]["entities"]
        )
        update_task_details(session, task_id=tid, title="Changed after proposal")
    with factory() as session:
        with pytest.raises(ConflictError, match="stale"):
            apply_context_action_proposal(session, store, settings, pid)


@pytest.mark.parametrize("output", [{}, {"proposals": []}])
def test_empty_generation_completes_run_and_releases_inbox(env_setup, output):
    assert _generate(env_setup, [], output=output) == []
    settings, store, factory, cid = env_setup
    with factory() as session:
        inbox = session.get(Inbox, "review-inbox")
        assert inbox.processing_status == "pending"
        assert inbox.processed_at is None
        run = session.scalar(
            select(ProcessingRun).where(ProcessingRun.run_type == "context_interpreter")
        )
        assert run.status == "completed" and run.finished_at is not None
        assert run.output_payload == {"proposals_count": 0, "resolution": "no_proposals"}
        assert not list(session.scalars(select(ContextActionProposal)))
        # The released item is usable, rather than merely displaying a new status.
        assert (
            propose_context_actions(
                session,
                context_id=cid,
                store=store,
                settings=settings,
                client=FakeProposalClient(),
                source_inbox_id=inbox.id,
            )
            == []
        )


def test_stale_reject_cannot_overwrite_applied_proposal_http409(env_setup):
    settings, store, factory, _ = env_setup
    [pid] = _generate(env_setup, [{"action": "create_topic", "title": "Applied topic"}])
    with factory() as stale:
        cached = stale.get(ContextActionProposal, pid)
        with factory() as other:
            apply_context_action_proposal(other, store, settings, pid)
        assert cached.status == "proposed"
        with pytest.raises(ConflictError):
            reject_context_action_proposal(stale, pid)
    with TestClient(create_app(settings)) as client:
        assert (
            client.post(
                f"/context-action-proposals/{pid}/reject", headers=auth_headers()
            ).status_code
            == 409
        )
    with factory() as session:
        assert session.get(ContextActionProposal, pid).status == "applied"
        assert session.get(Inbox, "review-inbox").processing_status == "processed"
        assert session.scalar(select(Topic).where(Topic.title == "Applied topic")) is not None


def _parallel(operations):
    barrier = threading.Barrier(len(operations))
    results, errors = [], []

    def worker(operation):
        try:
            barrier.wait(timeout=10)
            results.append(operation())
        except Exception as err:
            errors.append(err)

    threads = [threading.Thread(target=worker, args=(operation,)) for operation in operations]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()
    return results, errors


def test_parallel_apply_versus_reject_has_one_winner(env_setup):
    settings, store, factory, _ = env_setup
    [pid] = _generate(env_setup, [{"action": "create_topic", "title": "One winner"}])

    def apply():
        with factory() as session:
            return apply_context_action_proposal(session, store, settings, pid).status

    def reject():
        with factory() as session:
            return reject_context_action_proposal(session, pid).status

    results, errors = _parallel([apply, reject])
    assert len(results) == len(errors) == 1
    assert isinstance(errors[0], ConflictError)
    with factory() as session:
        winner = session.get(ContextActionProposal, pid).status
        assert winner == results[0]
        assert session.get(Inbox, "review-inbox").processing_status == (
            "processed" if winner == "applied" else "pending"
        )
        assert bool(session.scalar(select(Topic.id))) == (winner == "applied")


@pytest.mark.parametrize("apply_first", [False, True])
def test_parallel_sibling_resolution_uses_current_database_status(env_setup, apply_first):
    settings, store, factory, _ = env_setup
    pids = _generate(
        env_setup, [{"action": "create_topic", "title": f"Sibling {i}"} for i in range(2)]
    )
    loaded = threading.Barrier(2)

    def resolve(index):
        with factory() as session:
            # Both siblings are retained in each identity map before either resolves.
            cached = list(session.scalars(select(ContextActionProposal)))
            loaded.wait(timeout=10)
            if index == 0 and apply_first:
                return apply_context_action_proposal(session, store, settings, pids[index]).status
            return reject_context_action_proposal(session, pids[index]).status

    results, errors = _parallel([lambda: resolve(0), lambda: resolve(1)])
    assert not errors
    assert len(results) == 2
    with factory() as session:
        assert session.get(Inbox, "review-inbox").processing_status == (
            "processed" if apply_first else "pending"
        )
        assert sorted(session.scalars(select(ContextActionProposal.status))) == (
            ["applied", "rejected"] if apply_first else ["rejected", "rejected"]
        )


def test_change_between_preflight_and_claim_returns_http409(env_setup, monkeypatch):
    settings, store, factory, cid = env_setup
    with factory() as session:
        tid = create_topic(session, context_id=cid, title="Concurrent topic").id
    [pid] = _generate(
        env_setup, [{"action": "set_topic_status", "topic_id": tid, "status": "done"}]
    )
    original = context_actions.verify_workspace_manifest
    calls = []

    def verify(session, *args):
        calls.append(session.connection().connection.in_transaction)
        result = original(session, *args)
        if len(calls) == 1:
            with factory() as other:
                set_topic_status(other, topic_id=tid, status="blocked")
        return result

    monkeypatch.setattr(context_actions, "verify_workspace_manifest", verify)
    with TestClient(create_app(settings)) as client:
        response = client.post(f"/context-action-proposals/{pid}/apply", headers=auth_headers())
        assert response.status_code == 409, response.text
    assert calls == [False, True]  # Recheck takes place in a real SQLite write transaction.
    with factory() as session:
        assert session.get(Topic, tid).status == "blocked"
        assert session.get(ContextActionProposal, pid).status == "proposed"
        assert session.get(Inbox, "review-inbox").processing_status == "processing"
        assert not list(session.scalars(select(Event).where(Event.source_id == pid)))


@pytest.mark.parametrize("target", ["progress", "topic"])
@pytest.mark.parametrize("change", ["file", "entity"])
def test_final_knowledge_guard_after_intent_refuses_changes_without_ambiguity(
    env_setup, monkeypatch, target, change
):
    action, path = _knowledge(env_setup, target)
    [pid] = _generate(env_setup, [action])
    settings, store, factory, cid = env_setup
    original = (
        ContextKnowledgeService._record if target == "progress" else TopicKnowledgeService._intent
    )
    changed = []

    def inject(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        is_intent = (
            target == "topic" or kwargs.get("event_type") == "CONTEXT_KNOWLEDGE_WRITE_INTENT"
        )
        if is_intent and not changed:
            if change == "file":
                path.write_text(path.read_text() + "\nChanged during intent commit\n")
            else:
                with factory() as other:
                    if target == "topic":
                        set_topic_status(other, topic_id=action["topic_id"], status="blocked")
                    else:
                        update_context(other, context_id=cid, title="Changed context")
            changed.append(path.read_bytes())
        return result

    monkeypatch.setattr(
        ContextKnowledgeService if target == "progress" else TopicKnowledgeService,
        "_record" if target == "progress" else "_intent",
        inject,
    )
    with TestClient(create_app(settings)) as client:
        response = client.post(f"/context-action-proposals/{pid}/apply", headers=auth_headers())
        assert response.status_code == 409, response.text
    assert changed and path.read_bytes() == changed[0]
    with factory() as session:
        assert session.get(ContextActionProposal, pid).status == "proposed"
        assert session.get(Inbox, "review-inbox").processing_status == "processing"
        failures = list(
            session.scalars(
                select(Event).where(Event.source_id == pid, Event.event_type.like("%FAILED"))
            )
        )
        assert len(failures) == 1 and failures[0].payload["outcome"] == "not_written"


def test_knowledge_write_holds_store_lock_and_real_database_transaction(env_setup, monkeypatch):
    action, _ = _knowledge(env_setup, "progress")
    [pid] = _generate(env_setup, [action])
    settings, store, factory, _ = env_setup
    original = store.apply
    checked = []
    with factory() as session:

        def write(update):
            assert store._lock._is_owned()
            assert session.connection().connection.in_transaction
            # A second writer cannot mutate referents while the file is written.
            with factory() as other:
                other.execute(text("PRAGMA busy_timeout=0"))
                from sqlalchemy.exc import OperationalError

                with pytest.raises(OperationalError, match="locked"):
                    other.execute(text("UPDATE contexts SET title='rival'"))
            checked.append(True)
            return original(update)

        monkeypatch.setattr(store, "apply", write)
        assert apply_context_action_proposal(session, store, settings, pid).status == "applied"
    assert checked == [True]


@pytest.mark.parametrize("parser_cut,length", [(True, 100), (False, 5000)])
def test_server_provenance_keeps_parser_or_prompt_truncation(env_setup, parser_cut, length):
    settings, store, factory, cid = env_setup
    with factory() as session:
        resource = Resource(
            context_id=cid,
            locator="/example/doc.txt",
            relative_path="doc.txt",
            source_path_snapshot="/example",
            title="Snapshot",
            resource_type="text",
            status="active",
            current_content_hash="a" * 64,
            last_seen_at=datetime.now(UTC),
        )
        session.add(resource)
        session.flush()
        snap = ResourceSnapshot(
            resource_id=resource.id,
            content_hash="a" * 64,
            extractor_version="test",
            status="ready",
            text="x" * length,
            truncated=parser_cut,
            metadata_payload={},
        )
        session.add(snap)
        session.commit()
        client = FakeProposalClient(
            output_generator=json.dumps(
                {
                    "proposals": [
                        {
                            "action": {"action": "create_topic", "title": "Cut input"},
                            "evidence": {
                                "kind": "resource_snapshot",
                                "source_id": snap.id,
                                "content_hash": snap.content_hash,
                                "truncated": False,
                            },
                        }
                    ]
                }
            )
        )
        [p] = propose_context_actions(
            session,
            context_id=cid,
            store=store,
            settings=settings,
            client=client,
            resource_snapshot_id=snap.id,
        )
        assert p.preconditions["evidence"]["truncated"] is True
        assert '"truncated": true' in client.prompts[0]
        assert "Text wurde gekuerzt" in client.prompts[0]


@pytest.mark.parametrize("missing", ["packet", "manifest"])
def test_apply_also_refuses_persisted_task_proposals_without_delivered_version(env_setup, missing):
    settings, store, factory, cid = env_setup
    with factory() as session:
        tid = create_task(session, title="Selected task", context_id=cid).id
    [pid] = _generate(env_setup, [{"action": "link_task_context", "task_id": tid}])
    with factory() as session:
        p = session.get(ContextActionProposal, pid)
        if missing == "packet":
            p.packet_payload = {**p.packet_payload, "current_tasks": []}
        else:
            manifest = p.preconditions["manifest"]
            p.preconditions = {
                **p.preconditions,
                "manifest": {
                    **manifest,
                    "entities": [e for e in manifest["entities"] if e["entity_type"] != "task"],
                },
            }
        session.commit()
    with factory() as session:
        with pytest.raises(ConflictError, match="Task fehlt"):
            apply_context_action_proposal(session, store, settings, pid)
        assert session.get(ContextActionProposal, pid).status == "proposed"
