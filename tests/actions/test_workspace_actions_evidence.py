"""Evidence contracts for actions."""

from __future__ import annotations

from support.actions import FakeProposalClient, env_setup as env_setup
import json
import pytest
from organiser.db.models import (
    Resource,
    ResourceSnapshot,
)
from organiser.services.context_actions import (
    apply_context_action_proposal,
)
from organiser.services.context_interpreter import propose_context_actions
from organiser.services.domain import (
    ConflictError,
)


def test_propose_and_apply_snapshot_evidence_and_resource_links(env_setup, tmp_path):
    settings, store, factory, context_id = env_setup

    pdf_file = tmp_path / "doc.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 dummy pdf content")

    import hashlib

    pdf_hash = hashlib.sha256(pdf_file.read_bytes()).hexdigest()

    from support.ingestion import seed_source_id

    # Create resource and snapshot
    with factory() as session:
        source_id = seed_source_id(session, context_id, path=str(tmp_path))
        res = Resource(
            context_id=context_id,
            source_id=source_id,
            locator=str(pdf_file),
            relative_path="doc.pdf",
            source_path_snapshot=str(pdf_file),
            title="Dokument",
            resource_type="pdf",
            current_content_hash=pdf_hash,
            status="active",
        )
        session.add(res)
        session.flush()

        snap = ResourceSnapshot(
            resource_id=res.id,
            content_hash=pdf_hash,
            extractor_version="pdf-1.0",
            status="ready",
            text="Abschnittueberschrift: Portfoliotheorie. Risiko und Rendite.",
        )
        session.add(snap)
        session.commit()
        res_id, snap_id = res.id, snap.id

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Portfoliotheorie"},
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snap_id,
                            "content_hash": pdf_hash,
                        },
                    }
                ]
            }
        )
    )

    with factory() as session:
        proposals = propose_context_actions(
            session,
            context_id=context_id,
            store=store,
            settings=settings,
            client=client,
            resource_snapshot_id=snap_id,
        )
        assert len(proposals) == 1
        p_id = proposals[0].id

    with factory() as session:
        applied = apply_context_action_proposal(session, store, settings, p_id)
        assert applied.status == "applied"


def test_stale_resource_version_rejects_apply(env_setup, tmp_path):
    settings, store, factory, context_id = env_setup

    doc_file = tmp_path / "doc.md"
    doc_file.write_text("Originalinhalt", encoding="utf-8")

    from support.ingestion import seed_source_id

    import hashlib

    file_hash = hashlib.sha256(b"Originalinhalt").hexdigest()

    with factory() as session:
        source_id = seed_source_id(session, context_id, path=str(tmp_path))
        res = Resource(
            context_id=context_id,
            source_id=source_id,
            locator=str(doc_file),
            relative_path="doc.md",
            source_path_snapshot=str(doc_file),
            title="Dokument",
            resource_type="markdown",
            current_content_hash=file_hash,
            status="active",
        )
        session.add(res)
        session.flush()
        snap = ResourceSnapshot(
            resource_id=res.id,
            content_hash=file_hash,
            extractor_version="text-1.0",
            status="ready",
            text="Originalinhalt",
        )
        session.add(snap)
        session.commit()
        snap_id = snap.id

    client = FakeProposalClient(
        output_generator=json.dumps(
            {
                "proposals": [
                    {
                        "action": {"action": "create_topic", "title": "Thema aus Doc"},
                        "evidence": {
                            "kind": "resource_snapshot",
                            "source_id": snap_id,
                            "content_hash": file_hash,
                        },
                    }
                ]
            }
        )
    )

    with factory() as session:
        proposals = propose_context_actions(
            session,
            context_id=context_id,
            store=store,
            settings=settings,
            client=client,
            resource_snapshot_id=snap_id,
        )
        p_id = proposals[0].id

    # Now modify the file on disk so the hash changes!
    doc_file.write_text("Veraenderter Inhalt", encoding="utf-8")

    with factory() as session:
        with pytest.raises(ConflictError, match="stale oder nicht zugaenglich"):
            apply_context_action_proposal(session, store, settings, p_id)
