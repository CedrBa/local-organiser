"""HTTP surface of the resource registry. Router factory, no application.

``build_router(get_session, settings)`` returns a router that the integrator
mounts into the existing protected router, so the Bearer-token dependency stays
exactly where it is today and no second, unprotected application can appear. The
router therefore takes its session dependency as an argument and never creates
one itself.

Two rules shape every route:

* **IDs only.** No request may carry a filesystem path, a limit, a budget or a
  hash of the operator's choosing. A scan is addressed by context id and source
  id; extraction and verification by resource id. The verification route takes
  the expected content hash, because that is the value a proposal was built from
  and E needs to hand it back unchanged.
* **Codes, not stack traces.** Every refusal is answered with the stable
  ``ResourceIngestionError.code`` plus a short German detail. A raw exception text
  from the filesystem or the parser never reaches a client.
"""

from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from organiser.core.config import Settings
from organiser.db.models import ContextSource
from organiser.domain.resources import (
    ResourceDetail,
    ResourceIngestionError,
    ResourceListPage,
    ResourceVersionCheck,
    ExtractionResult,
    ScanResult,
)
from organiser.services.resources import (
    extract_resource,
    get_resource_snapshot,
    list_resources,
    scan_source,
    verify_resource_version,
)


class ResourceVersionRequest(BaseModel):
    """Body of the freshness check. ``extra="forbid"``: no hidden knobs."""

    model_config = ConfigDict(extra="forbid")

    expected_hash: str = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")


def _fail(error: ResourceIngestionError) -> HTTPException:
    return HTTPException(status_code=error.status_code, detail=error.as_payload())


def _call(operation, *args, **kwargs):
    """Run a service call and translate its coded refusals into HTTP answers."""
    try:
        return operation(*args, **kwargs)
    except ResourceIngestionError as error:
        raise _fail(error) from error


def _require_owned_source(session: Session, context_id: str, source_id: str) -> None:
    """Refuse a source that does not belong to the addressed context.

    The check is explicit instead of relying on the scan: a client must not be
    able to scan context A's source through context B's URL and learn about it.

    The read transaction is closed again before returning. The service behind the
    route owns its transactions and must not inherit an open one -- and no lock
    may be held while the scan reads files.
    """
    source = session.get(ContextSource, source_id)
    if source is None or source.context_id != context_id:
        session.rollback()
        raise HTTPException(
            status_code=404,
            detail={
                "code": "source_not_found",
                "detail": "Die Quelle existiert in diesem Context nicht",
            },
        )
    session.rollback()


def build_router(get_session: Callable[..., Session], settings: Settings) -> APIRouter:
    """Build the resource router for the protected application router."""
    router = APIRouter()

    @router.get("/contexts/{context_id}/resources", response_model=ResourceListPage)
    def read_context_resources(
        context_id: str,
        source_id: str | None = None,
        status: str | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0, le=1_000_000),
        session: Session = Depends(get_session),
    ) -> ResourceListPage:
        """Read context resources through the protected GET /contexts/{context_id}/resources endpoint."""
        return _call(
            list_resources,
            session,
            context_id,
            source_id=source_id,
            status=status,
            limit=limit,
            offset=offset,
        )

    @router.get("/resources/{resource_id}", response_model=ResourceDetail)
    def read_resource(resource_id: str, session: Session = Depends(get_session)) -> ResourceDetail:
        """Read resource through the protected GET /resources/{resource_id} endpoint."""
        return _call(get_resource_snapshot, session, resource_id)

    @router.post("/contexts/{context_id}/sources/{source_id}/scan", response_model=ScanResult)
    def run_source_scan(
        context_id: str, source_id: str, session: Session = Depends(get_session)
    ) -> ScanResult:
        """Run source scan through the protected POST /contexts/{context_id}/sources/{source_id}/scan endpoint."""
        _require_owned_source(session, context_id, source_id)
        return _call(scan_source, session, source_id, settings)

    @router.post("/resources/{resource_id}/extract", response_model=ExtractionResult)
    def run_resource_extraction(
        resource_id: str, session: Session = Depends(get_session)
    ) -> ExtractionResult:
        """Run resource extraction through the protected POST /resources/{resource_id}/extract endpoint."""
        return _call(extract_resource, session, resource_id, settings)

    @router.post("/resources/{resource_id}/verify", response_model=ResourceVersionCheck)
    def run_resource_version_check(
        resource_id: str, request: ResourceVersionRequest, session: Session = Depends(get_session)
    ) -> ResourceVersionCheck:
        """Run resource version check through the protected POST /resources/{resource_id}/verify endpoint."""
        return _call(verify_resource_version, session, resource_id, request.expected_hash, settings)

    return router
