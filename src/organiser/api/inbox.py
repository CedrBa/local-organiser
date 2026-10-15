"""HTTP inbox operations; authentication is inherited from the protected parent router."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from organiser.domain.proposals import ManualTriageRequest, TriageRunResult
from organiser.services.inbox import store_inbox_item
from organiser.services.interpreter import interpret_inbox
from organiser.services.local_llm import OpenAICompatibleInterpreterClient
from organiser.services.triage import apply_manual_proposal
from organiser.api.inbox_input import InboxItem


def build_router(app_settings, get_database_session, llm_client) -> APIRouter:
    """Build routes using explicit application-owned dependencies."""
    router = APIRouter()

    @router.post("/inbox")
    def add_to_inbox(
        item: InboxItem, session: Session = Depends(get_database_session)
    ) -> dict[str, str]:
        """Add to inbox through the protected POST /inbox endpoint."""
        stored = store_inbox_item(
            session,
            item_id=item.id,
            created_at=item.created_at,
            text=item.text,
            raw_payload=item.model_dump(mode="json"),
        )
        return {"status": "stored" if stored else "already_exists", "id": item.id}

    @router.post("/inbox/{inbox_id}/triage", response_model=TriageRunResult)
    def triage_inbox_item(
        inbox_id: str,
        request: ManualTriageRequest,
        session: Session = Depends(get_database_session),
    ):
        """Triage inbox item through the protected POST /inbox/{inbox_id}/triage endpoint."""
        if request.proposal.source_inbox_id != inbox_id:
            raise HTTPException(
                status_code=422, detail="source_inbox_id muss der Inbox-URL entsprechen"
            )
        return apply_manual_proposal(session, request.proposal)

    @router.post("/inbox/{inbox_id}/interpret", response_model=TriageRunResult)
    def interpret_inbox_item(inbox_id: str, session: Session = Depends(get_database_session)):
        """Interpret inbox item through the protected POST /inbox/{inbox_id}/interpret endpoint."""
        client = llm_client or OpenAICompatibleInterpreterClient(app_settings)
        return interpret_inbox(session, inbox_id=inbox_id, client=client)

    return router
