import json

import pytest
from sqlalchemy import select

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Event, PlannerProposalRecord, Task
from organiser.db.session import create_session_factory
from organiser.services.domain import ConflictError, create_project, create_task
from organiser.services.planner import apply_planning_proposal, create_planning_proposal


class FakePlanner:
    model_name = "fake-planner"

    def __init__(self, response: str):
        self.response = response
        self.prompt = ""

    def plan(self, prompt: str) -> str:
        self.prompt = prompt
        return self.response


@pytest.fixture
def session(tmp_path):
    settings = Settings(database_path=tmp_path / "planner.sqlite3")
    upgrade_database(settings)
    session_factory = create_session_factory(settings)
    with session_factory() as database_session:
        yield database_session
        database_session.rollback()


def proposal(target_type: str, target_id: str, actions: list[dict]) -> str:
    return json.dumps(
        {
            "target_type": target_type,
            "target_id": target_id,
            "summary": "Naechste sinnvolle Schritte",
            "actions": actions,
        }
    )


def action(kind: str, rationale: str = "Hilft beim Fortschritt", **fields) -> dict:
    return {"action": kind, "rationale": rationale, "missing_fields": [], **fields}


def test_planner_persists_context_raw_output_and_validated_proposal_without_writing_domain(session):
    project = create_project(session, title="Hausarbeit")
    raw = proposal(
        "project",
        project.id,
        [action("create_task", title="Gliederung erstellen", project_id=project.id)],
    )
    planner = FakePlanner(raw)

    result = create_planning_proposal(
        session, target_type="project", target_id=project.id, client=planner
    )

    assert result.status == "proposed"
    assert result.raw_output == raw
    assert result.proposal.actions[0].title == "Gliederung erstellen"
    assert result.context["kind"] == "planning_context"
    assert "Context Packet" in planner.prompt
    assert session.scalars(select(Task)).all() == []
    assert session.get(PlannerProposalRecord, result.id).model == "fake-planner"


def test_apply_planner_proposal_uses_domain_services_and_is_idempotent(session):
    project = create_project(session, title="Hausarbeit")
    raw = proposal(
        "project",
        project.id,
        [action("create_task", title="Gliederung erstellen", project_id=project.id)],
    )
    proposed = create_planning_proposal(
        session, target_type="project", target_id=project.id, client=FakePlanner(raw)
    )

    applied = apply_planning_proposal(session, proposal_id=proposed.id)

    assert applied.status == "applied"
    task = session.scalar(select(Task))
    assert task.title == "Gliederung erstellen"
    event = session.scalar(select(Event).where(Event.entity_id == task.id))
    assert event.source_type == "planner_proposal"
    assert event.source_id == proposed.id
    with pytest.raises(ConflictError):
        apply_planning_proposal(session, proposal_id=proposed.id)


def test_invalid_or_out_of_scope_plan_is_persisted_as_failed_without_domain_change(session):
    target = create_task(session, title="Ziel")
    other = create_task(session, title="Anderes Ziel")
    raw = proposal("task", target.id, [action("complete_task", target_task_id=other.id)])

    result = create_planning_proposal(
        session, target_type="task", target_id=target.id, client=FakePlanner(raw)
    )

    assert result.status == "failed"
    assert result.raw_output == raw
    assert "angefragten Task" in result.error
    assert session.get(Task, target.id).status == "open"
    assert session.get(Task, other.id).status == "open"


def test_task_plan_can_create_only_direct_child_task(session):
    target = create_task(session, title="Steuererklaerung")
    raw = proposal(
        "task", target.id, [action("create_task", title="Belege sammeln", parent_task_id=target.id)]
    )
    proposed = create_planning_proposal(
        session, target_type="task", target_id=target.id, client=FakePlanner(raw)
    )

    applied = apply_planning_proposal(session, proposal_id=proposed.id)

    assert applied.status == "applied"
    child = session.scalar(select(Task).where(Task.parent_task_id == target.id))
    assert child.title == "Belege sammeln"


def test_uncertain_plan_is_visible_but_cannot_be_applied(session):
    target = create_task(session, title="Unklares Ziel")
    raw = proposal(
        "task", target.id, [action("complete_task", target_task_id=target.id, uncertainty=0.4)]
    )

    proposed = create_planning_proposal(
        session, target_type="task", target_id=target.id, client=FakePlanner(raw)
    )
    applied = apply_planning_proposal(session, proposal_id=proposed.id)

    assert proposed.status == "proposed"
    assert applied.status == "failed"
    assert session.get(Task, target.id).status == "open"
