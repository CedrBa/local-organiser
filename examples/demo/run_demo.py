"""Exercise the real service pipeline using synthetic data and a labelled fake model.

Every run owns a temporary database, knowledge store and source directory. It
neither reads environment settings nor contacts a model/calendar endpoint.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sqlalchemy import func, select

from organiser.core.config import Settings
from organiser.db.migrations import upgrade_database
from organiser.db.models import Resource, ResourceSnapshot, Task
from organiser.db.session import create_session_factory
from organiser.services.context_actions import apply_context_action_proposal
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.context_knowledge import ContextKnowledgeService, shared_knowledge_store
from organiser.services.domain import add_context_source, create_context
from organiser.services.resources import scan_source
from organiser.services.workspace_context import workspace_context


class DemoProposalClient:
    """Return a fixed proposal for the supplied synthetic snapshot; this is not an LLM."""

    model_name = "synthetic-demo-fake-no-model"

    def __init__(self, snapshot_id: str, content_hash: str, resource_id: str) -> None:
        self.snapshot_id = snapshot_id
        self.content_hash = content_hash
        self.resource_id = resource_id

    def propose_context(self, prompt: str) -> str:
        """Emit a typed task proposal with real server-generated evidence identifiers."""
        return json.dumps(
            {
                "proposals": [
                    {
                        "action": {
                            "action": "create_task",
                            "title": "Review the Fourier introduction exercises",
                            "resource_ids": [self.resource_id],
                            "estimated_minutes": 20,
                        },
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": self.snapshot_id,
                            "content_hash": self.content_hash,
                        },
                    }
                ]
            }
        )


def run_demo(*, apply: bool = False) -> None:
    """Show scan, bounded workspace and persisted proposal; apply only when requested."""
    with TemporaryDirectory(prefix="local-organiser-demo-") as directory:
        root = Path(directory)
        source = root / "sources"
        source.mkdir()
        document = source / "fourier.md"
        document.write_text(
            "# Fourier introduction\n\nReview the introduction exercises. "
            "The remaining practice takes approximately 20 minutes.\n",
            encoding="utf-8",
        )
        original_hash = hashlib.sha256(document.read_bytes()).hexdigest()
        settings = Settings(
            database_path=root / "demo.sqlite3",
            knowledge_root=root / "knowledge",
            source_allowed_roots=(source.resolve(),),
            api_token="synthetic-demo-token-not-a-credential",
        )
        upgrade_database(settings)
        factory = create_session_factory(settings)
        store = shared_knowledge_store(settings)

        with factory() as session:
            context_id = create_context(
                session, title="Synthetic mathematics course", type="course"
            ).id
        with factory() as session:
            ContextKnowledgeService(session, store).ensure_context_knowledge(context_id)
        with factory() as session:
            source_id = add_context_source(
                session, context_id=context_id, source_type="folder", path=str(source)
            ).id
        with factory() as session:
            scan = scan_source(session, source_id, settings)
            assert scan.complete, "Synthetic source scan must be complete"
        with factory() as session:
            resource = session.scalar(select(Resource).where(Resource.context_id == context_id))
            snapshot = session.scalar(
                select(ResourceSnapshot).where(ResourceSnapshot.resource_id == resource.id)
            )
            assert snapshot.status == "ready"
            model = DemoProposalClient(snapshot.id, snapshot.content_hash, resource.id)
            snapshot_id = snapshot.id
            packet = workspace_context(session, context_id, store)
            print(
                f"1. Scanned synthetic source; workspace size: {len(packet.model_dump_json())} chars"
            )

        with factory() as session:
            proposals = propose_context_actions(
                session,
                context_id=context_id,
                store=store,
                settings=settings,
                client=model,
                resource_snapshot_id=snapshot_id,
            )
            assert len(proposals) == 1
            proposal_id = proposals[0].id
            print("2. FAKE MODEL proposal, not yet applied:")
            print(json.dumps(proposals[0].action_payload, indent=2))
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Task)) == 0
        print("3. Confirmed: proposal generation created no task.")

        if apply:
            with factory() as session:
                result = apply_context_action_proposal(session, store, settings, proposal_id)
                assert result.status == "applied"
            with factory() as session:
                assert session.scalar(select(func.count()).select_from(Task)) == 1
            print("4. Explicit --apply: proposal applied; exactly one task created.")
        else:
            print(
                "4. Review only. Use --apply to demonstrate confirmed application in a fresh run."
            )
        assert hashlib.sha256(document.read_bytes()).hexdigest() == original_hash
        print("5. Original source unchanged. Temporary demo data is removed on exit.")
        factory.kw["bind"].dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true", help="Apply the displayed synthetic proposal"
    )
    run_demo(apply=parser.parse_args().apply)
