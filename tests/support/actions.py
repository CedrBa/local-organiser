"""Isolated action-service environment and deterministic fake model client."""

from __future__ import annotations
import pytest
from conftest import make_settings
from organiser.db.migrations import upgrade_database
from organiser.db.session import create_session_factory
from organiser.services.context_knowledge import (
    shared_knowledge_store,
)
from organiser.services.domain import (
    create_context,
)


class FakeProposalClient:
    def __init__(self, output_generator=None, model_name: str = "fake-model"):
        self.model_name = model_name
        self._output_generator = output_generator or (lambda prompt: '{"proposals": []}')
        self.prompts: list[str] = []

    def propose_context(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if callable(self._output_generator):
            return self._output_generator(prompt)
        return self._output_generator


@pytest.fixture
def env_setup(tmp_path):
    settings = make_settings(
        tmp_path,
        "actions.sqlite3",
        knowledge_root=tmp_path / "knowledge",
        source_allowed_roots=(tmp_path.resolve(),),
    )
    upgrade_database(settings)
    store = shared_knowledge_store(settings)
    factory = create_session_factory(settings)

    with factory() as session:
        ctx = create_context(session, title="Finanzierung", type="course")
        context_id = ctx.id
        session.commit()

    return settings, store, factory, context_id
