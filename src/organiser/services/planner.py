"""Explicit local planning: model text becomes a persisted proposal, never a direct write."""

from datetime import UTC, datetime
import json

from sqlalchemy.orm import Session

from organiser.db.models import PlannerProposalRecord
from organiser.domain.context import ContextPacket
from organiser.domain.proposals import PlanningAction, PlanningProposal, PlanningProposalResult
from organiser.services.context import planning_context
from organiser.services.knowledge import KnowledgeStore
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
    cancel_task,
    complete_task,
    create_task,
    get_task,
    reschedule_task,
    start_task,
    update_task_details,
)
from organiser.services.local_llm import InterpreterClientError, PlannerClient


class PlannerOutputError(Exception):
    pass


class PlannerProposalValidationError(DomainValidationError):
    pass


def build_planner_prompt(context: ContextPacket) -> str:
    """Describe the explicit target and bounded context for a reviewable task plan."""
    return f"""Heute ist {datetime.now(UTC).date().isoformat()}. Erstelle einen kleinen, vorsichtigen Plan fuer genau dieses Ziel.
Gib ausschliesslich ein JSON-Objekt ohne Markdown oder Erklaerung aus.
Du darfst nur diese Aktionen vorschlagen: create_task, update_task, start_task, complete_task, cancel_task, reschedule_task.
Schema: target_type (task|project), target_id, summary, actions (1 bis 3 Eintraege). Jeder Action-Eintrag benoetigt action; fuege eine kurze rationale und nur bei Bedarf target_task_id, title, description, project_id, parent_task_id, earliest_start_at, due_at, estimated_minutes, confidence, uncertainty oder missing_fields hinzu. target_task_id fehlt bei create_task.
Unklare oder riskante Schritte nicht als sichere Aktion ausgeben: uncertainty oder missing_fields setzen. Verwende ausschliesslich IDs aus dem Context Packet. Plane keine Kalenderbloecke und keine Hintergrundverarbeitung.
Context Packet:
{context.model_dump_json()}"""


def _validate_action(action: PlanningAction, *, for_apply: bool) -> None:
    if for_apply and (action.uncertainty or action.missing_fields):
        raise PlannerProposalValidationError(
            "Unsichere Planner-Aktionen duerfen nicht angewendet werden"
        )
    if action.action == "create_task":
        if not action.title:
            raise PlannerProposalValidationError("create_task benoetigt title")
        if action.target_task_id is not None:
            raise PlannerProposalValidationError("create_task darf keine target_task_id enthalten")
        return
    if not action.target_task_id:
        raise PlannerProposalValidationError("Task-Aktionen benoetigen target_task_id")
    if action.action == "update_task" and all(
        value is None
        for value in (
            action.title,
            action.description,
            action.project_id,
            action.parent_task_id,
            action.estimated_minutes,
        )
    ):
        raise PlannerProposalValidationError(
            "update_task benoetigt mindestens ein geaendertes Feld"
        )
    if (
        action.action == "reschedule_task"
        and action.earliest_start_at is None
        and action.due_at is None
    ):
        raise PlannerProposalValidationError("reschedule_task benoetigt einen Zeitstempel")


def _validate_proposal(proposal: PlanningProposal, *, target_type: str, target_id: str) -> None:
    if proposal.target_type != target_type or proposal.target_id != target_id:
        raise PlannerProposalValidationError(
            "Planner-Output referenziert nicht das angefragte Ziel"
        )
    for action in proposal.actions:
        _validate_action(action, for_apply=False)
        if target_type == "task":
            if action.action != "create_task" and action.target_task_id != target_id:
                raise PlannerProposalValidationError(
                    "Task-Plan darf nur den angefragten Task veraendern"
                )
            if action.action == "create_task" and action.parent_task_id != target_id:
                raise PlannerProposalValidationError(
                    "Neue Tasks eines Task-Plans muessen direkte Child-Tasks sein"
                )


def _result(record: PlannerProposalRecord) -> PlanningProposalResult:
    proposal = (
        PlanningProposal.model_validate(record.proposal_payload)
        if record.proposal_payload
        else None
    )
    return PlanningProposalResult(
        id=record.id,
        status=record.status,
        target_type=record.target_type,
        target_id=record.target_id,
        model=record.model,
        context_event_max_id=record.context_event_max_id,
        context=record.context_payload,
        raw_output=record.raw_output,
        proposal=proposal,
        error=record.error,
    )


def get_planning_proposal(session: Session, proposal_id: str) -> PlanningProposalResult:
    """Read a persisted planning proposal without applying its actions."""
    record = session.get(PlannerProposalRecord, proposal_id)
    if record is None:
        raise NotFoundError("Planner-Proposal nicht gefunden")
    return _result(record)


def create_planning_proposal(
    session: Session,
    *,
    target_type: str,
    target_id: str,
    client: PlannerClient,
    store: KnowledgeStore | None = None,
) -> PlanningProposalResult:
    """Request and persist a validated task/project plan without changing domain state."""
    context = planning_context(session, target_type=target_type, target_id=target_id, store=store)
    record = PlannerProposalRecord(
        target_type=target_type,
        target_id=target_id,
        status="failed",
        model=client.model_name,
        context_event_max_id=context.context_event_max_id,
        context_payload=context.model_dump(mode="json"),
    )
    session.add(record)
    session.commit()
    try:
        raw_output = client.plan(build_planner_prompt(context))
        proposal = PlanningProposal.model_validate(json.loads(raw_output))
        _validate_proposal(proposal, target_type=target_type, target_id=target_id)
        record.raw_output = raw_output
        record.proposal_payload = proposal.model_dump(mode="json")
        record.status = "proposed"
        session.commit()
    except (
        InterpreterClientError,
        json.JSONDecodeError,
        ValueError,
        PlannerProposalValidationError,
    ) as error:
        session.rollback()
        record = session.get(PlannerProposalRecord, record.id)
        if record is None:
            raise
        record.raw_output = locals().get("raw_output")
        record.error = f"Ungueltiges Planner-Proposal: {error}"
        record.status = "failed"
        session.commit()
    return _result(record)


def _apply_action(session: Session, action: PlanningAction, context: EventContext) -> None:
    if action.action == "create_task":
        create_task(
            session,
            title=action.title or "",
            description=action.description,
            project_id=action.project_id,
            parent_task_id=action.parent_task_id,
            earliest_start_at=action.earliest_start_at,
            due_at=action.due_at,
            estimated_minutes=action.estimated_minutes,
            context=context,
        )
    elif action.action == "update_task":
        changes = {
            field: getattr(action, field)
            for field in (
                "title",
                "description",
                "estimated_minutes",
                "project_id",
                "parent_task_id",
            )
            if getattr(action, field) is not None
        }
        update_task_details(
            session, task_id=action.target_task_id or "", context=context, **changes
        )
    elif action.action == "start_task":
        start_task(session, task_id=action.target_task_id or "", context=context)
    elif action.action == "complete_task":
        complete_task(session, task_id=action.target_task_id or "", context=context)
    elif action.action == "cancel_task":
        cancel_task(session, task_id=action.target_task_id or "", context=context)
    else:
        reschedule_task(
            session,
            task_id=action.target_task_id or "",
            earliest_start_at=action.earliest_start_at,
            due_at=action.due_at,
            context=context,
        )


def apply_planning_proposal(session: Session, *, proposal_id: str) -> PlanningProposalResult:
    """Claim and apply a validated plan once, committing its domain changes together."""
    record = session.get(PlannerProposalRecord, proposal_id)
    if record is None:
        raise NotFoundError("Planner-Proposal nicht gefunden")
    if record.status != "proposed":
        raise ConflictError("Nur ein neues Planner-Proposal kann angewendet werden")
    try:
        proposal = PlanningProposal.model_validate(record.proposal_payload)
        _validate_proposal(proposal, target_type=record.target_type, target_id=record.target_id)
        for action in proposal.actions:
            _validate_action(action, for_apply=True)
        if record.target_type == "project":
            for action in proposal.actions:
                if action.action != "create_task":
                    task = get_task(session, action.target_task_id or "")
                    if task.project_id != record.target_id:
                        raise PlannerProposalValidationError(
                            "Project-Plan darf nur Tasks des angefragten Projects veraendern"
                        )
                elif action.project_id != record.target_id:
                    raise PlannerProposalValidationError(
                        "Neue Tasks eines Project-Plans muessen diesem Project zugeordnet sein"
                    )
        event_context = EventContext(
            source_type="planner_proposal", source_id=record.id, correlation_id=record.id
        )
        session.info["defer_domain_commit"] = True
        try:
            for action in proposal.actions:
                _apply_action(session, action, event_context)
        finally:
            session.info.pop("defer_domain_commit", None)
        record.status = "applied"
        record.applied_at = datetime.now(UTC)
        session.commit()
    except (DomainValidationError, NotFoundError, ConflictError, ValueError) as error:
        session.rollback()
        record = session.get(PlannerProposalRecord, proposal_id)
        if record is None:
            raise
        record.status = "failed"
        record.error = f"Planner-Proposal wurde nicht angewendet: {error}"
        session.commit()
    return _result(record)
