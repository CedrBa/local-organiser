"""Manual, validated bridge from inbox proposals to domain services."""

from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from organiser.db.models import Event, Inbox, ProcessingRun
from organiser.domain.proposals import InterpretationProposal, TriageRunResult
from organiser.services import domain
from organiser.services.domain import (
    ConflictError,
    DomainValidationError,
    EventContext,
    NotFoundError,
)


class ProposalValidationError(DomainValidationError):
    pass


def validate_proposal(proposal: InterpretationProposal) -> None:
    """Reject incomplete or uncertain legacy proposals before executing any action."""
    if proposal.uncertainty or proposal.missing_fields:
        raise ProposalValidationError(
            "Proposal ist unvollstaendig oder unsicher und kann nicht angewendet werden"
        )
    if proposal.action in {"create_task", "create_project"} and not proposal.title:
        raise ProposalValidationError("title ist fuer diese Aktion erforderlich")
    if (
        proposal.action
        in {"update_task", "start_task", "complete_task", "cancel_task", "reschedule_task"}
        and not proposal.target_entity_id
    ):
        raise ProposalValidationError("target_entity_id ist fuer diese Aktion erforderlich")
    if proposal.action == "update_task" and all(
        value is None
        for value in (
            proposal.title,
            proposal.description,
            proposal.estimated_minutes,
            proposal.project_id,
            proposal.parent_task_id,
        )
    ):
        raise ProposalValidationError("update_task benoetigt mindestens ein zu aenderndes Feld")
    if (
        proposal.action == "reschedule_task"
        and proposal.earliest_start_at is None
        and proposal.due_at is None
    ):
        raise ProposalValidationError("reschedule_task benoetigt mindestens einen Zeitstempel")


def proposal_event_context(run: ProcessingRun, proposal: InterpretationProposal) -> EventContext:
    """Derive audit provenance from the inbox and processing run without writing state."""
    return EventContext(
        source_type="inbox",
        source_id=proposal.source_inbox_id,
        correlation_id=proposal.correlation_id or run.id,
    )


def apply_proposal_actions(
    session: Session, proposal: InterpretationProposal, context: EventContext
) -> tuple[str, str]:
    """Execute an already validated legacy action through domain services.

    The caller must validate and set the application transaction boundary first;
    this helper does not claim the inbox or independently validate model output."""
    if proposal.action == "create_task":
        entity = domain.create_task(
            session,
            title=proposal.title or "",
            description=proposal.description,
            project_id=proposal.project_id,
            parent_task_id=proposal.parent_task_id,
            earliest_start_at=proposal.earliest_start_at,
            due_at=proposal.due_at,
            estimated_minutes=proposal.estimated_minutes,
            source_inbox_id=proposal.source_inbox_id,
            context=context,
        )
        return "task", entity.id
    if proposal.action == "create_project":
        entity = domain.create_project(
            session, title=proposal.title or "", description=proposal.description, context=context
        )
        return "project", entity.id

    task_id = proposal.target_entity_id or ""
    if proposal.action == "update_task":
        changes = {
            field: getattr(proposal, field)
            for field in (
                "title",
                "description",
                "estimated_minutes",
                "project_id",
                "parent_task_id",
            )
            if getattr(proposal, field) is not None
        }
        entity = domain.update_task_details(session, task_id=task_id, context=context, **changes)
    elif proposal.action == "start_task":
        entity = domain.start_task(session, task_id=task_id, context=context)
    elif proposal.action == "complete_task":
        entity = domain.complete_task(session, task_id=task_id, context=context)
    elif proposal.action == "cancel_task":
        entity = domain.cancel_task(session, task_id=task_id, context=context)
    elif proposal.action == "reschedule_task":
        entity = domain.reschedule_task(
            session,
            task_id=task_id,
            earliest_start_at=proposal.earliest_start_at,
            due_at=proposal.due_at,
            context=context,
        )
    else:
        raise ProposalValidationError("Nicht unterstuetzte Proposal-Aktion")
    return "task", entity.id


def _record_failed_run(
    session: Session, proposal: InterpretationProposal, error: Exception
) -> TriageRunResult:
    session.rollback()
    inbox = session.get(Inbox, proposal.source_inbox_id)
    if inbox is None:
        raise NotFoundError("Inbox-Eintrag nicht gefunden") from error
    run = ProcessingRun(
        run_type="manual_triage",
        status="failed",
        source_inbox_id=inbox.id,
        finished_at=datetime.now(UTC),
        output_payload=proposal.model_dump(mode="json"),
        error=str(error),
    )
    inbox.processing_status = "pending"
    inbox.last_error = str(error)
    session.add(run)
    session.commit()
    return TriageRunResult(run_id=run.id, status=run.status, source_inbox_id=inbox.id)


def apply_manual_proposal(session: Session, proposal: InterpretationProposal) -> TriageRunResult:
    """Apply one explicit proposal. The proposal itself never writes SQL directly."""
    claim = session.execute(
        update(Inbox)
        .where(Inbox.id == proposal.source_inbox_id, Inbox.processing_status == "pending")
        .values(processing_status="processing", last_error=None)
    )
    if claim.rowcount == 0:
        inbox = session.get(Inbox, proposal.source_inbox_id)
        if inbox is None:
            raise NotFoundError("Inbox-Eintrag nicht gefunden")
        if inbox.processing_status == "processed":
            raise ConflictError("Inbox-Eintrag wurde bereits verarbeitet")
        if inbox.processing_status == "processing":
            raise ConflictError("Inbox-Eintrag wird bereits verarbeitet")
        raise ConflictError(f"Inbox-Eintrag hat ungueltigen Status: {inbox.processing_status}")

    inbox = session.get(Inbox, proposal.source_inbox_id)
    try:
        validate_proposal(proposal)

        context_event_max_id = session.scalar(select(func.max(Event.id)))
        run = ProcessingRun(
            run_type="manual_triage",
            status="running",
            source_inbox_id=inbox.id,
            started_at=datetime.now(UTC),
            context_event_max_id=context_event_max_id,
        )
        session.add(run)
        session.flush()

        session.info["defer_domain_commit"] = True
        try:
            entity_type, entity_id = apply_proposal_actions(
                session, proposal, proposal_event_context(run, proposal)
            )
        finally:
            session.info.pop("defer_domain_commit", None)

        run.status = "completed"
        run.finished_at = datetime.now(UTC)
        run.output_payload = proposal.model_dump(mode="json")
        inbox.processing_status = "processed"
        inbox.processed_at = datetime.now(UTC)
        inbox.last_error = None
        session.commit()
        return TriageRunResult(
            run_id=run.id,
            status=run.status,
            source_inbox_id=inbox.id,
            entity_type=entity_type,
            entity_id=entity_id,
        )
    except (DomainValidationError, NotFoundError, ConflictError) as error:
        return _record_failed_run(session, proposal, error)
    except Exception:
        session.rollback()
        raise
