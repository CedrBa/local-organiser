"""Explicit local interpretation of one pending inbox entry."""

from datetime import UTC, datetime
import json

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from organiser.db.models import Event, Inbox, ProcessingRun
from organiser.domain.proposals import InterpretationProposal, TriageRunResult
from organiser.services.domain import ConflictError, NotFoundError, list_projects, list_tasks
from organiser.services.local_llm import InterpreterClient, InterpreterClientError
from organiser.services.triage import (
    ProposalValidationError,
    apply_proposal_actions,
    proposal_event_context,
    validate_proposal,
)


class InterpreterOutputError(Exception):
    pass


def _entity_context(session: Session) -> str:
    tasks = (
        list_tasks(session, has_deadline=True)[:10] + list_tasks(session, has_deadline=False)[:10]
    )
    projects = list_projects(session, status="active")[:10]
    task_lines = [
        f"- task id={task.id}; title={task.title}; status={task.status}; due_at={task.due_at}"
        for task in tasks
    ]
    project_lines = [
        f"- project id={project.id}; title={project.title}; status={project.status}"
        for project in projects
    ]
    return "\n".join(
        ["Tasks:", *(task_lines or ["- none"]), "Projects:", *(project_lines or ["- none"])]
    )


def build_interpreter_prompt(inbox: Inbox, context: str) -> str:
    """Build bounded legacy triage input; output still requires proposal validation."""
    return f"""Heute ist {datetime.now(UTC).date().isoformat()}. Interpretiere genau einen Inbox-Eintrag.
Gib ausschliesslich ein JSON-Objekt aus, ohne Markdown oder Erklaerung.
Erlaubte action-Werte: create_task, update_task, start_task, complete_task, cancel_task, reschedule_task, create_project.
Schema: source_inbox_id (string), action, target_entity_id (string|null), title (string|null), description (string|null), project_id (string|null), parent_task_id (string|null), earliest_start_at (ISO-8601|null), due_at (ISO-8601|null), estimated_minutes (integer|null), confidence (0..1|null), uncertainty (string|null), missing_fields (string[]), correlation_id (string|null).
Bei nicht eindeutiger bestehender Entity niemals raten: uncertainty und missing_fields setzen; target_entity_id dann null.
Fuer neue Aufgaben create_task, fuer neue Projekte create_project verwenden. Verwende nur IDs aus dem Kontext.
Inbox-ID: {inbox.id}
Inbox-Text: {inbox.text}
{context}"""


def _parse_proposal(raw_output: str, inbox_id: str) -> InterpretationProposal:
    try:
        proposal = InterpretationProposal.model_validate(json.loads(raw_output))
    except (json.JSONDecodeError, ValueError) as error:
        raise InterpreterOutputError(
            f"Modelloutput ist kein gueltiges Proposal: {error}"
        ) from error
    if proposal.source_inbox_id != inbox_id:
        raise InterpreterOutputError("Modelloutput referenziert eine andere Inbox-ID")
    return proposal


def _finish_failure(
    session: Session, *, run_id: str, inbox_id: str, error: Exception, raw_output: str | None = None
) -> TriageRunResult:
    session.rollback()
    run = session.get(ProcessingRun, run_id)
    inbox = session.get(Inbox, inbox_id)
    if run is None or inbox is None:
        raise NotFoundError("Interpreter-Run oder Inbox-Eintrag nicht gefunden") from error
    run.status = "failed"
    run.finished_at = datetime.now(UTC)
    run.error = str(error)
    if raw_output is not None:
        run.output_payload = {"raw_output": raw_output}
    inbox.processing_status = "pending"
    inbox.last_error = str(error)
    session.commit()
    return TriageRunResult(run_id=run.id, status=run.status, source_inbox_id=inbox.id)


def interpret_inbox(
    session: Session, *, inbox_id: str, client: InterpreterClient
) -> TriageRunResult:
    """Interpret one pending inbox entry; no background processing is started."""
    claim = session.execute(
        update(Inbox)
        .where(Inbox.id == inbox_id, Inbox.processing_status == "pending")
        .values(processing_status="processing", last_error=None)
    )
    if claim.rowcount == 0:
        inbox = session.get(Inbox, inbox_id)
        if inbox is None:
            raise NotFoundError("Inbox-Eintrag nicht gefunden")
        if inbox.processing_status == "processed":
            raise ConflictError("Inbox-Eintrag wurde bereits verarbeitet")
        if inbox.processing_status == "processing":
            raise ConflictError("Inbox-Eintrag wird bereits verarbeitet")
        raise ConflictError(f"Inbox-Eintrag hat ungueltigen Status: {inbox.processing_status}")

    inbox = session.get(Inbox, inbox_id)
    run = ProcessingRun(
        run_type="local_interpreter",
        status="running",
        source_inbox_id=inbox.id,
        model=client.model_name,
        started_at=datetime.now(UTC),
        context_event_max_id=session.scalar(select(func.max(Event.id))),
    )
    session.add(run)
    session.commit()

    raw_output: str | None = None
    try:
        raw_output = client.interpret(build_interpreter_prompt(inbox, _entity_context(session)))
        proposal = _parse_proposal(raw_output, inbox.id)
        validate_proposal(proposal)

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
    except (
        InterpreterClientError,
        InterpreterOutputError,
        ProposalValidationError,
        ConflictError,
        NotFoundError,
    ) as error:
        return _finish_failure(
            session, run_id=run.id, inbox_id=inbox.id, error=error, raw_output=raw_output
        )
    except Exception:
        session.rollback()
        raise
