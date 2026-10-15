"""Router factory for topic CRUD, topic/task-resource links and knowledge ensure.

This module builds an :class:`APIRouter` and nothing else. It deliberately does
**not** create an app:

* The existing protected app in :mod:`organiser.api.app` owns authentication,
  the session factory, the error handlers and the store. Mounting this router
  there is the integrator's serial step, which is also what keeps the token
  dependency and the 404/409/422 handlers consistent with the existing routes.
* A standalone unprotected app would be an unauthenticated product endpoint.

Every dependency is injected as ``get_session`` / ``get_store``, so the router
carries no import-time configuration and no filesystem or settings dependency.

Only identifiers
----------------

Every path parameter is an entity id, and every body field is a value, never a
location. There is deliberately no endpoint that accepts a path, a filename, a
storage key or a percentage for the database:

* ``POST /contexts/{context_id}/topics`` -- create a topic; the knowledge file
  address is derived from the generated key.
* ``GET  /contexts/{context_id}/topics``
* ``GET|PATCH /topics/{topic_id}`` -- PATCH renames; the key cannot be addressed.
* ``POST /topics/{topic_id}/status``
* ``GET /topics/{topic_id}/resources``, ``POST|DELETE`` with resource ID in path
* ``GET|POST /tasks/{task_id}/resources``, ``POST|DELETE`` with resource ID in path
* ``POST /contexts/{context_id}/knowledge/ensure`` -- context knowledge ensure.
* ``POST /topics/{topic_id}/knowledge/ensure`` -- topic knowledge ensure.
* ``POST /topics/{topic_id}/knowledge`` -- qualitative topic update.

Optional audit provenance
------------------------

Every mutating route takes an **optional** event-context body. It is a bare object
(``{"source_id": "..."}``), exactly like the link routes that carry the ID in the
path, and it is purely descriptive: a request **without** a body behaves
identically to before and audits with ``source_id IS NULL``. With a body the
values land in every audit event the operation writes, which is what lets an E
apply be attributed unambiguously via ``source_id = proposal.id``.

The ensure routes use a **strict** event-context model, so the nested spelling
``{"event_context": {...}}`` is rejected with ``422`` instead of being silently
dropped. A dropped provenance would produce an unattributed apply that looks
attributed, which is worse than a visible rejection.

Status codes
------------

Link POSTs answer ``201`` whether or not the link already existed. That is a
deliberate, documented choice, not an oversight: the routes are idempotent
(``POST``/``DELETE`` on the same pair converge), the body describes the current
state of the pair and not a creation, and no ``Location`` header is sent. A client
that needs to know whether something changed reads the audit events or re-reads
the link list -- ``201`` deliberately does not claim "I created something".
Changing this to ``200`` on the repeat would be a breaking contract change for
callers that key off ``201``, and there is no consumer that needs it today.

**A status code is not a retry permission.** In particular ``503`` does *not* mean
"the same request may succeed later": the legacy context ensure can answer ``503``
after a *partial* filesystem write or a failed audit completion, and the persisted
state is then genuinely unknown. There is no code here that authorises an automatic
retry. Every knowledge failure needs a deliberate recovery decision based on the
file and the audit log; the typed error classes exist to make that decision, not to
skip it. See ``TopicKnowledgeContentError`` for the non-transient case.

The last two knowledge endpoints run filesystem work and therefore require a
clean session. ``get_session`` yields a freshly opened session per request, which
is exactly what :class:`~organiser.services.topic_knowledge.TopicKnowledgeService`
and
:class:`~organiser.services.context_knowledge.ContextKnowledgeService` demand. A
caller that reuses one session across a DB mutation and a knowledge call will be
refused with a domain error -- deliberately, so foreign work can never be
committed along with the file operation.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from organiser.api.schemas import (
    EventContextInput,
    ResourceLinkCreate,
    ResourceLinkResponse,
    StrictEventContextInput,
    TaskResourceResponse,
    TopicCreate,
    TopicKnowledgeUpdateRequest,
    TopicResponse,
    TopicStatusUpdate,
    TopicUpdate,
)
from organiser.services.context_knowledge import (
    ContextKnowledgeAmbiguousError,
    ContextKnowledgeError,
    ContextKnowledgeNotInitializedError,
    ContextKnowledgeService,
)
from organiser.services.domain import DomainValidationError, EventContext
from organiser.services.knowledge import KnowledgeStore
from organiser.services.topic_knowledge import TopicKnowledgeContentError, TopicKnowledgeService
from organiser.services.topics import (
    create_topic,
    get_topic,
    link_resource_topic,
    link_task_resource,
    list_task_resources,
    list_topic_resources,
    list_topics,
    set_topic_status,
    unlink_resource_topic,
    unlink_task_resource,
    update_topic,
)


def _event_context(value: EventContextInput | None) -> EventContext | None:
    return EventContext(**value.model_dump()) if value is not None else None


def _knowledge_call(method, *args, **kwargs):
    """Translate internal failures without leaking filesystem/exception text.

    Order is part of the contract. ``TopicKnowledgeContentError`` must precede
    ``ContextKnowledgeError``, and ``TopicKnowledgeValidationError`` (which is
    both a domain and a knowledge error) must precede both -- otherwise a
    user-fixable markdown file would answer 503, which every client reads as
    "retry later" even though no retry can ever succeed.
    """
    try:
        return method(*args, **kwargs)
    except TopicKnowledgeContentError as error:
        # Non-transient: a human has to edit the file. Never 503, never a retry.
        raise HTTPException(422, detail={"code": TopicKnowledgeContentError.code}) from error
    except DomainValidationError as error:
        raise HTTPException(422, detail={"code": "invalid_topic_knowledge_update"}) from error
    except ContextKnowledgeAmbiguousError as error:
        raise HTTPException(
            409, detail={"code": "knowledge_ambiguous", "needs_review": True}
        ) from error
    except ContextKnowledgeNotInitializedError as error:
        raise HTTPException(409, detail={"code": "knowledge_not_initialized"}) from error
    except ContextKnowledgeError as error:
        raise HTTPException(503, detail={"code": "knowledge_operation_failed"}) from error


def build_router(
    get_session: Callable[[], Session],
    get_store: Callable[[], KnowledgeStore],
) -> APIRouter:
    """Build the workspace topic/task-link router.

    ``get_session`` is a FastAPI dependency yielding a session; ``get_store``
    yields the shared :class:`KnowledgeStore`. Both are provided by the hosting
    app so authentication, session lifetime and the single-writer store stay in
    one place.
    """
    router = APIRouter()

    # -- topics -------------------------------------------------------------

    @router.get("/contexts/{context_id}/topics", response_model=list[TopicResponse])
    def read_topics(
        context_id: str, status: str | None = None, session: Session = Depends(get_session)
    ):
        """Read topics through the protected GET /contexts/{context_id}/topics endpoint."""
        return list_topics(session, context_id, status=status)

    @router.post("/contexts/{context_id}/topics", response_model=TopicResponse, status_code=201)
    def add_topic(context_id: str, request: TopicCreate, session: Session = Depends(get_session)):
        # Nur die Datenbank wird angelegt. Die Wissensdatei entsteht erst durch
        # den ausdruecklichen ensure-Endpunkt.
        """Add topic through the protected POST /contexts/{context_id}/topics endpoint."""
        return create_topic(
            session,
            context_id=context_id,
            title=request.title,
            status=request.status,
            context=_event_context(request.event_context),
        )

    @router.get("/topics/{topic_id}", response_model=TopicResponse)
    def read_topic(topic_id: str, session: Session = Depends(get_session)):
        """Read topic through the protected GET /topics/{topic_id} endpoint."""
        return get_topic(session, topic_id)

    @router.patch("/topics/{topic_id}", response_model=TopicResponse)
    def patch_topic(topic_id: str, request: TopicUpdate, session: Session = Depends(get_session)):
        """Patch topic through the protected PATCH /topics/{topic_id} endpoint."""
        values = request.model_dump(exclude={"event_context"}, exclude_unset=True)
        return update_topic(
            session, topic_id=topic_id, context=_event_context(request.event_context), **values
        )

    @router.post("/topics/{topic_id}/status", response_model=TopicResponse)
    def patch_topic_status(
        topic_id: str, request: TopicStatusUpdate, session: Session = Depends(get_session)
    ):
        """Patch topic status through the protected POST /topics/{topic_id}/status endpoint."""
        return set_topic_status(
            session,
            topic_id=topic_id,
            status=request.status,
            context=_event_context(request.event_context),
        )

    # -- topic <-> resource --------------------------------------------------

    @router.get("/topics/{topic_id}/resources", response_model=list[ResourceLinkResponse])
    def read_topic_resources(topic_id: str, session: Session = Depends(get_session)):
        """Read topic resources through the protected GET /topics/{topic_id}/resources endpoint."""
        return list_topic_resources(session, topic_id)

    @router.post(
        "/topics/{topic_id}/resources/{resource_id}", response_model=TopicResponse, status_code=201
    )
    def add_topic_resource(
        topic_id: str,
        resource_id: str,
        event_context: EventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
    ):
        """Add topic resource through the protected POST /topics/{topic_id}/resources/{resource_id} endpoint."""
        link_resource_topic(
            session,
            topic_id=topic_id,
            resource_id=resource_id,
            context=_event_context(event_context),
        )
        return get_topic(session, topic_id)

    @router.delete("/topics/{topic_id}/resources/{resource_id}", response_model=TopicResponse)
    def remove_topic_resource(
        topic_id: str,
        resource_id: str,
        event_context: EventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
    ):
        """Remove topic resource through the protected DELETE /topics/{topic_id}/resources/{resource_id} endpoint."""
        unlink_resource_topic(
            session,
            topic_id=topic_id,
            resource_id=resource_id,
            context=_event_context(event_context),
        )
        return get_topic(session, topic_id)

    # -- task <-> resource ---------------------------------------------------

    @router.get("/tasks/{task_id}/resources", response_model=list[ResourceLinkResponse])
    def read_task_resources(task_id: str, session: Session = Depends(get_session)):
        """Read task resources through the protected GET /tasks/{task_id}/resources endpoint."""
        return list_task_resources(session, task_id)

    @router.post("/tasks/{task_id}/resources", response_model=TaskResourceResponse, status_code=201)
    def add_task_resource(
        task_id: str, request: ResourceLinkCreate, session: Session = Depends(get_session)
    ):
        """Add task resource through the protected POST /tasks/{task_id}/resources endpoint."""
        link = link_task_resource(
            session,
            task_id=task_id,
            resource_id=request.resource_id,
            context=_event_context(request.event_context),
        )
        return TaskResourceResponse(task_id=link.task_id, resource_id=link.resource_id)

    @router.post(
        "/tasks/{task_id}/resources/{resource_id}",
        response_model=TaskResourceResponse,
        status_code=201,
    )
    def add_task_resource_by_id(
        task_id: str,
        resource_id: str,
        event_context: EventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
    ):
        """Add task resource by id through the protected POST /tasks/{task_id}/resources/{resource_id} endpoint."""
        link = link_task_resource(
            session, task_id=task_id, resource_id=resource_id, context=_event_context(event_context)
        )
        return TaskResourceResponse(task_id=link.task_id, resource_id=link.resource_id)

    @router.delete("/tasks/{task_id}/resources/{resource_id}", status_code=204)
    def remove_task_resource(
        task_id: str,
        resource_id: str,
        event_context: EventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
    ):
        """Remove task resource through the protected DELETE /tasks/{task_id}/resources/{resource_id} endpoint."""
        unlink_task_resource(
            session, task_id=task_id, resource_id=resource_id, context=_event_context(event_context)
        )
        return Response(status_code=204)

    # -- knowledge ----------------------------------------------------------

    @router.post("/contexts/{context_id}/knowledge/ensure")
    def ensure_context_store(
        context_id: str,
        event_context: StrictEventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
        store: KnowledgeStore = Depends(get_store),
    ):
        # Optionaler, rein beschreibender Body: ohne Body bleibt das Verhalten
        # unveraendert (source_id=None). Mit Body wandert die Provenienz in jeden
        # Audit-Ereignis der Operation -- fuer E ``source_id=proposal.id``.
        """Ensure context store through the protected POST /contexts/{context_id}/knowledge/ensure endpoint."""
        result = _knowledge_call(
            ContextKnowledgeService(session, store).ensure_context_knowledge,
            context_id,
            context=_event_context(event_context),
        )
        return {
            "context_id": result.context_id,
            "knowledge_path": result.knowledge_path,
            "bound_now": result.bound_now,
            "created_files": list(result.created_files),
            "changed": result.changed,
            "operation_id": result.operation_id,
        }

    @router.post("/topics/{topic_id}/knowledge/ensure")
    def ensure_topic_store(
        topic_id: str,
        event_context: StrictEventContextInput | None = Body(default=None),
        session: Session = Depends(get_session),
        store: KnowledgeStore = Depends(get_store),
    ):
        """Ensure topic store through the protected POST /topics/{topic_id}/knowledge/ensure endpoint."""
        result = _knowledge_call(
            TopicKnowledgeService(session, store).ensure_topic_knowledge,
            topic_id,
            context=_event_context(event_context),
        )
        return {
            "topic_id": result.topic_id,
            "context_id": result.context_id,
            "knowledge_key": result.knowledge_key,
            "relative_file": result.relative_file,
            "created": result.created,
            "context_bound_now": result.context_bound_now,
            "operation_id": result.operation_id,
        }

    @router.post("/topics/{topic_id}/knowledge")
    def update_topic_store(
        topic_id: str,
        request: TopicKnowledgeUpdateRequest,
        session: Session = Depends(get_session),
        store: KnowledgeStore = Depends(get_store),
    ):
        """Update topic store through the protected POST /topics/{topic_id}/knowledge endpoint."""
        write = _knowledge_call(
            TopicKnowledgeService(session, store).apply_topic_update,
            topic_id,
            text=request.text,
            percent=request.percent,
            context=_event_context(request.event_context),
        )
        return {
            "topic_id": write.topic_id,
            "knowledge_key": write.knowledge_key,
            "relative_file": write.relative_file,
            "changed": write.changed,
            "content_hash": write.content_hash,
            "operation_id": write.operation_id,
        }

    return router


__all__ = ["build_router"]
