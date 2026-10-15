"""Read-only HTTP access to a context workspace.

``build_router`` returns a plain ``APIRouter`` **without** its own auth
dependency. The protected application in ``organiser.api.app`` mounts it, so the
workspace endpoint inherits exactly the same bearer-token check as every other
business route. There is deliberately no second, unprotected app here and no
unprotected side app: authentication is the integrator's single decision point and
duplicating it here would create a route without it.

One endpoint, GET ``/contexts/{context_id}/workspace``. It is a pure read: no body,
no query action, no background work, no model call. In particular there is no "ask
the assistant about this context" endpoint -- that is a later, separate decision and
it must not grow out of a read model.

The limits are not request-controlled. ``DEFAULT_WORKSPACE_POLICY`` is passed
unchanged; a client cannot ask for a bigger packet, and there is no path parameter
that could widen what is read from disk.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from organiser.domain.context import ContextWorkspacePacket
from organiser.services.knowledge import KnowledgeStore
from organiser.services.workspace_context import DEFAULT_WORKSPACE_POLICY, workspace_context


def build_router(
    get_session: Callable[[], Iterator], get_store: Callable[[], KnowledgeStore]
) -> APIRouter:
    """Router factory; the integrator injects the session and knowledge store.

    ``get_session`` is the application's own session dependency and ``get_store``
    its shared knowledge store, so the endpoint uses the same single-writer store
    instance as the write path without this module knowing how either is built.
    """
    router = APIRouter()

    @router.get("/contexts/{context_id}/workspace", response_model=ContextWorkspacePacket)
    def get_context_workspace(
        context_id: str,
        topic_id: Annotated[list[str] | None, Query()] = None,
        resource_id: Annotated[list[str] | None, Query()] = None,
        session=Depends(get_session),
        store=Depends(get_store),
    ) -> ContextWorkspacePacket:
        """Bounded, evidence-backed overview of one context.

        ``topic_id`` and ``resource_id`` are optional explicit focus filters. They
        are hard filters, not hints: an ID that is unknown, or that belongs to a
        different context, is refused with 422 instead of being resolved by
        similarity or silently dropped.
        """
        return workspace_context(
            session,
            context_id,
            store,
            DEFAULT_WORKSPACE_POLICY,
            topic_ids=tuple(topic_id or ()),
            resource_ids=tuple(resource_id or ()),
        )

    return router
