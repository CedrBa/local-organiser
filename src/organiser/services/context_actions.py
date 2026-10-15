"""Application and rejection service for context-bound action proposals."""

from __future__ import annotations

from datetime import UTC, datetime
import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from organiser.core.config import Settings
from organiser.db.models import (
    ContextActionProposal,
    Inbox,
)
from organiser.domain.knowledge import ContextKnowledgeUpdate
from organiser.domain.context_actions import knowledge_target_path
from organiser.domain.workspace_contracts import WorkspaceManifest
from organiser.services.context_knowledge import (
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeService,
)
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
    create_task,
    get_task,
    update_task_details,
)
from organiser.services.knowledge import KnowledgeStore
from organiser.services.resources import verify_resource_version
from organiser.services.topic_knowledge import (
    TopicKnowledgeAmbiguousError,
    TopicKnowledgeService,
)
from organiser.services.topics import (
    create_topic,
    link_resource_topic,
    link_task_resource,
    set_topic_status,
)
from organiser.services.workspace_context import verify_workspace_manifest

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _update_inbox_status_after_resolution(session: Session, inbox_id: str) -> None:
    """Update inbox processing status according to Decision A1 & A4.

    If any proposal of this inbox is still 'proposed' or 'applying', inbox stays 'processing'.
    If any proposal is 'failed' or 'ambiguous', inbox stays 'processing' (needs manual review, A4).
    If all proposals are resolved:
      - If at least one was 'applied': inbox becomes 'processed'
      - If zero proposals were applied (all 'rejected'): inbox reverts to 'pending' (A1)
    """
    inbox = session.get(Inbox, inbox_id)
    if inbox is None:
        return

    # Flush our resolution, then read columns rather than cached ORM proposals.
    # The conditional claim holds SQLite's write lock across this decision.
    session.flush()
    statuses = list(
        session.scalars(
            select(ContextActionProposal.status).where(
                ContextActionProposal.source_inbox_id == inbox_id
            )
        )
    )
    if not statuses:
        return

    if any(s in ("proposed", "applying") for s in statuses):
        return

    if any(s in ("failed", "ambiguous") for s in statuses):
        return

    any_applied = any(s == "applied" for s in statuses)
    if any_applied:
        inbox.processing_status = "processed"
        inbox.processed_at = _utc_now()
    else:
        inbox.processing_status = "pending"
        inbox.processed_at = None


def get_context_action_proposal(session: Session, proposal_id: str) -> ContextActionProposal:
    """Read one persisted context action without applying or regenerating it."""
    proposal = session.get(ContextActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError("Proposal nicht gefunden")
    return proposal


def list_context_action_proposals(
    session: Session,
    context_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[ContextActionProposal]:
    """Read persisted context actions using explicit context/source/status filters."""
    stmt = (
        select(ContextActionProposal)
        .where(ContextActionProposal.context_id == context_id)
        .order_by(ContextActionProposal.created_at.desc(), ContextActionProposal.id)
        .limit(limit)
        .offset(offset)
    )
    if status is not None:
        stmt = stmt.where(ContextActionProposal.status == status)
    return list(session.scalars(stmt))


def reject_context_action_proposal(session: Session, proposal_id: str) -> ContextActionProposal:
    """Explicitly reject one proposal; releases inbox if all proposals are rejected."""
    proposal = session.get(ContextActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError("Proposal nicht gefunden")
    try:
        _claim_proposal(session, proposal_id, "rejected")
        proposal = session.get(ContextActionProposal, proposal_id)
        if proposal.source_inbox_id:
            _update_inbox_status_after_resolution(session, proposal.source_inbox_id)
        session.commit()
        return proposal
    except Exception:
        session.rollback()
        raise


def _claim_proposal(session: Session, proposal_id: str, status: str = "applying") -> None:
    # UPDATE starts a real pysqlite write transaction, unlike a preceding SELECT.
    claim = session.execute(
        update(ContextActionProposal)
        .where(ContextActionProposal.id == proposal_id, ContextActionProposal.status == "proposed")
        .values(status=status)
        .execution_options(synchronize_session=False)
    )
    if claim.rowcount != 1:
        raise ConflictError("Proposal ist nicht mehr im Status 'proposed'")
    session.expire_all()


def _verify_preconditions(
    session: Session, store: KnowledgeStore | None, proposal: ContextActionProposal
) -> None:
    try:
        manifest = WorkspaceManifest.model_validate(proposal.preconditions["manifest"])
    except (KeyError, TypeError, ValueError) as err:
        raise DomainValidationError("Precondition-Manifest fehlt oder ist beschaedigt") from err
    context_entry = next((e for e in manifest.entities if e.entity_type == "context"), None)
    if context_entry is None or context_entry.entity_id != proposal.context_id:
        raise ConflictError("Context-Vorbedingung fehlt oder passt nicht zum Proposal")
    if proposal.action_payload.get("action") == "link_task_context":
        task_id = proposal.action_payload.get("task_id")
        delivered = (proposal.packet_payload or {}).get("current_tasks", [])
        if not any(t.get("id") == task_id for t in delivered) or not any(
            e.entity_type == "task" and e.entity_id == task_id for e in manifest.entities
        ):
            raise ConflictError("Task fehlt im gelieferten Packet oder im Precondition-Manifest")
    if proposal.action_payload.get("action") == "update_knowledge":
        try:
            target_path = knowledge_target_path(
                context_entry.selected_fields.get("knowledge_path"),
                proposal.action_payload.get("target"),
                proposal.action_payload.get("topic_id"),
            )
        except ValueError as err:
            raise ConflictError("Knowledge-Ziel ist nicht kanonisch gebunden") from err
        if not any(
            k.context_id == proposal.context_id
            and k.relative_path == target_path
            and k.content_hash
            for k in manifest.knowledge
        ):
            raise ConflictError("Knowledge-Dateiversion fehlt im Precondition-Manifest")
    verification = verify_workspace_manifest(session, store, manifest)
    if verification.changes:
        details = "; ".join(f"{c.kind}:{c.reference}.{c.field}" for c in verification.changes[:3])
        raise ConflictError(f"Kontext oder referenzierte Entitaeten sind stale ({details})")


class _PreconditionConflict(ConflictError):
    """The final guard refused the write; no filesystem mutation was attempted."""


def apply_context_action_proposal(
    session: Session,
    store: KnowledgeStore | None,
    settings: Settings,
    proposal_id: str,
) -> ContextActionProposal:
    """Verify preconditions and apply one context action proposal."""
    proposal = session.get(ContextActionProposal, proposal_id)
    if proposal is None:
        raise NotFoundError("Proposal nicht gefunden")
    if proposal.status != "proposed":
        raise ConflictError(
            f"Proposal hat Status '{proposal.status}' und kann nicht angewendet werden"
        )

    # Files are checked before claiming: B owns its transaction boundaries and
    # must never roll back our claim. This first manifest check is only preflight.
    _verify_preconditions(session, store, proposal)
    resource_check = (proposal.preconditions or {}).get("resource_check")
    if resource_check:
        session.rollback()
        check = verify_resource_version(
            session, resource_check["resource_id"], resource_check["expected_hash"], settings
        )
        if not check.ok:
            raise ConflictError(
                f"Quelldokument ist stale oder nicht zugaenglich: {check.reason} (Status: {check.resource_status})"
            )
    session.rollback()
    proposal = session.get(ContextActionProposal, proposal_id)

    action = proposal.action_payload
    action_type = action.get("action")

    # Lock order is store -> DB, matching TopicKnowledgeService's write path.
    if action_type == "update_knowledge":
        if store is None:
            raise DomainValidationError("Kein KnowledgeStore verfuegbar")
        with store._lock:
            try:
                _claim_proposal(session, proposal_id)
                proposal = session.get(ContextActionProposal, proposal_id)
                _verify_preconditions(session, store, proposal)
                session.commit()  # Durable claim before any filesystem write.
            except Exception:
                session.rollback()
                raise
            return _apply_knowledge_proposal(session, store, proposal_id)

    # 3. DB Actions: atomic conditional claim + domain action in one transaction
    try:
        _claim_proposal(session, proposal_id)
        proposal = session.get(ContextActionProposal, proposal_id)
        _verify_preconditions(session, store, proposal)
    except Exception:
        session.rollback()
        raise

    session.info["defer_domain_commit"] = True
    event_ctx = EventContext(source_type="context_action_proposal", source_id=proposal_id)
    try:
        result_payload: dict[str, Any] = {}
        if action_type == "create_topic":
            title = action["title"]
            new_topic = create_topic(
                session, context_id=proposal.context_id, title=title, context=event_ctx
            )
            result_payload = {
                "topic_id": new_topic.id,
                "title": new_topic.title,
                "knowledge_key": new_topic.knowledge_key,
            }

        elif action_type == "set_topic_status":
            top_id = action["topic_id"]
            top_status = action["status"]
            updated_top = set_topic_status(
                session, topic_id=top_id, status=top_status, context=event_ctx
            )
            result_payload = {"topic_id": updated_top.id, "status": updated_top.status}

        elif action_type == "link_resource_topic":
            top_id = action["topic_id"]
            res_id = action["resource_id"]
            link_resource_topic(session, topic_id=top_id, resource_id=res_id, context=event_ctx)
            result_payload = {"topic_id": top_id, "resource_id": res_id}

        elif action_type == "create_task":
            title = action["title"]
            description = action.get("description")
            top_id = action.get("topic_id")
            res_ids = action.get("resource_ids") or []
            due_at = action.get("due_at")
            if due_at and isinstance(due_at, str):
                due_at = datetime.fromisoformat(due_at)
            earliest_start_at = action.get("earliest_start_at")
            if earliest_start_at and isinstance(earliest_start_at, str):
                earliest_start_at = datetime.fromisoformat(earliest_start_at)
            estimated_minutes = action.get("estimated_minutes")
            cognitive_load = action.get("cognitive_load") or "medium"

            new_task = create_task(
                session,
                title=title,
                description=description,
                context_id=proposal.context_id,
                topic_id=top_id,
                due_at=due_at,
                earliest_start_at=earliest_start_at,
                estimated_minutes=estimated_minutes,
                cognitive_load=cognitive_load,
                context=event_ctx,
            )
            for rid in res_ids:
                link_task_resource(session, task_id=new_task.id, resource_id=rid, context=event_ctx)
            result_payload = {
                "task_id": new_task.id,
                "title": new_task.title,
                "context_id": new_task.context_id,
                "topic_id": new_task.topic_id,
                "resource_ids": list(res_ids),
            }

        elif action_type == "link_task_context":
            tsk_id = action["task_id"]
            top_id = action.get("topic_id")
            task_row = get_task(session, tsk_id)
            if task_row.context_id is not None and task_row.context_id != proposal.context_id:
                raise ConflictError(f"Task ist bereits Kontext '{task_row.context_id}' zugeordnet")
            update_task_details(
                session,
                task_id=tsk_id,
                context_id=proposal.context_id,
                topic_id=top_id,
                context=event_ctx,
            )
            result_payload = {
                "task_id": tsk_id,
                "context_id": proposal.context_id,
                "topic_id": top_id,
            }

        else:
            raise DomainValidationError(f"Unbekannter Aktionstyp '{action_type}'")

        proposal = session.get(ContextActionProposal, proposal_id)
        proposal.status = "applied"
        proposal.applied_at = _utc_now()
        proposal.result_payload = result_payload

        if proposal.source_inbox_id:
            _update_inbox_status_after_resolution(session, proposal.source_inbox_id)

        session.commit()
        return proposal

    except Exception:
        session.rollback()
        raise
    finally:
        session.info.pop("defer_domain_commit", None)


def _apply_knowledge_proposal(
    session: Session,
    store: KnowledgeStore,
    proposal_id: str,
) -> ContextActionProposal:
    # The caller retains the shared store lock until completion. The services
    # commit their own intent first, then invoke this guard immediately before
    # writing. BEGIN IMMEDIATE holds DB versions through write and audit completion.
    proposal = session.get(ContextActionProposal, proposal_id)
    action = proposal.action_payload
    context_id = proposal.context_id
    session.rollback()

    def before_write() -> None:
        session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        session.expire_all()
        try:
            _verify_preconditions(session, store, session.get(ContextActionProposal, proposal_id))
        except ConflictError as err:
            raise _PreconditionConflict(str(err)) from err

    event_ctx = EventContext(source_type="context_action_proposal", source_id=proposal_id)
    target = action.get("target")
    text = action.get("text")
    heading = action.get("heading")
    percent = action.get("percent")
    topic_id = action.get("topic_id")

    try:
        if target == "topic":
            if not topic_id:
                raise DomainValidationError("topic_id fehlt fuer target='topic'")
            service = TopicKnowledgeService(session, store)
            write = service.apply_topic_update(
                topic_id=topic_id,
                text=text,
                percent=percent,
                context=event_ctx,
                before_write=before_write,
            )
            result = {
                "operation_id": write.operation_id,
                "relative_path": write.relative_file,
                "changed": write.changed,
            }
        elif target in ("overview", "progress", "note"):
            action_map = {
                "overview": "update_overview",
                "progress": "update_progress",
                "note": "append_note",
            }
            k_action = action_map[target]
            update_dto = ContextKnowledgeUpdate(
                context_id=context_id,
                action=k_action,
                text=text,
                heading=heading,
                percent=percent,
            )
            service = ContextKnowledgeService(session, store)
            write = service.apply(update_dto, context=event_ctx, before_write=before_write)
            result = {
                "operation_id": write.operation_id,
                "relative_path": write.file,
                "changed": write.changed,
            }
        else:
            raise DomainValidationError(f"Unbekanntes Knowledge-Target '{target}'")

        proposal = session.get(ContextActionProposal, proposal_id)
        proposal.status = "applied"
        proposal.applied_at = _utc_now()
        proposal.result_payload = result
        if proposal.source_inbox_id:
            _update_inbox_status_after_resolution(session, proposal.source_inbox_id)
        session.commit()
        return proposal

    except _PreconditionConflict:
        session.rollback()
        session.execute(
            update(ContextActionProposal)
            .where(
                ContextActionProposal.id == proposal_id, ContextActionProposal.status == "applying"
            )
            .values(status="proposed")
        )
        session.commit()
        raise

    except (ContextKnowledgeAmbiguousError, TopicKnowledgeAmbiguousError) as amb_err:
        session.rollback()
        proposal = session.get(ContextActionProposal, proposal_id)
        if proposal is not None:
            proposal.status = "ambiguous"
            proposal.error_code = "knowledge_ambiguous"
            proposal.result_payload = {"error": str(amb_err)}
            session.commit()
        raise ConflictError(
            f"Knowledge-Schreibvorgang mehrdeutig/unterbrochen: {amb_err}"
        ) from amb_err

    except Exception as write_err:
        session.rollback()
        proposal = session.get(ContextActionProposal, proposal_id)
        if proposal is not None:
            proposal.status = "ambiguous"
            proposal.error_code = "write_failure"
            proposal.result_payload = {"error": str(write_err)}
            session.commit()
        raise ConflictError(f"Fehler bei Knowledge-Schreibvorgang: {write_err}") from write_err
