"""Persist intent and outcomes through an explicit service commit callback."""

from __future__ import annotations

from typing import Any, Callable
from sqlalchemy.orm import Session
from organiser.services.domain import (
    EventContext,
    record_event,
)
from .contracts import ContextKnowledgeError, _KNOWLEDGE_EVENTS


class KnowledgeAudit:
    """Persist correlated intent and outcomes using an explicit commit callback."""

    def __init__(self, session: Session, commit: Callable[[], None]) -> None:
        self._session = session
        self._commit = commit

    def _record(
        self,
        *,
        event_type: str,
        context_id: str,
        payload: dict[str, Any],
        event_context: EventContext | None,
        wrap_commit_error: bool = True,
    ) -> None:
        """Schreibe ein Audit-Ereignis und committe es sofort."""
        if event_type not in _KNOWLEDGE_EVENTS:
            raise ContextKnowledgeError(f"Unbekanntes Knowledge-Ereignis: {event_type}")
        record_event(
            self._session,
            event_type=event_type,
            entity_type="context",
            entity_id=context_id,
            payload=payload,
            context=event_context,
        )
        try:
            self._commit()
        except Exception as error:
            if not wrap_commit_error:
                raise
            raise ContextKnowledgeError(
                f"Audit-Ereignis {event_type} konnte nicht gespeichert werden: {error}"
            ) from error

    def _record_if_reachable(
        self,
        *,
        event_type: str,
        context_id: str,
        payload: dict[str, Any],
        event_context: EventContext | None,
    ) -> bool:
        """Fehlerereignis, aber nur soweit die Datenbank noch erreichbar ist.

        Ein nicht persistierbares Failure-Ereignis darf die eigentliche
        Fehlermeldung nicht verschlucken und wird deshalb still ignoriert.
        """
        try:
            self._record(
                event_type=event_type,
                context_id=context_id,
                payload=payload,
                event_context=event_context,
            )
        except Exception:
            return False
        return True
