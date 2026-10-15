"""Detached source/resource identities and accumulated scan observations."""

from __future__ import annotations

from dataclasses import dataclass, field
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    ContextSource,
    Resource,
)
from organiser.domain.resources import (
    CODE_SOURCE_INACTIVE,
    CODE_SOURCE_TYPE_UNSUPPORTED,
    ContextNotFoundError,
    ResourceNotFoundError,
    ScanCounters,
    SourceAccessError,
    SourceNotFoundError,
    canonical_locator,
)
from .constants import SCANNABLE_SOURCE_TYPES, STATUS_ACTIVE


# -- internal identities -----------------------------------------------------


@dataclass(frozen=True)
class SourceIdentity:
    """The source facts a decision depends on, copied out of the session."""

    source_id: str
    context_id: str
    source_type: str
    path: str
    status: str
    locator: str

    def identity_tuple(self) -> tuple[str, str, str, str]:
        return (self.source_id, self.context_id, self.source_type, self.path)


@dataclass(frozen=True)
class ResourceIdentity:
    """The resource facts a decision depends on, copied out of the session."""

    resource_id: str
    context_id: str
    source_id: str | None
    locator: str
    source_path_snapshot: str
    relative_path: str
    title: str
    resource_type: str
    status: str
    current_content_hash: str | None

    def identity_tuple(self) -> tuple[str, str, str | None, str]:
        return (self.resource_id, self.context_id, self.source_id, self.locator)


@dataclass
class _ScanState:
    counters: dict[str, int] = field(default_factory=dict)
    warnings: dict[str, int] = field(default_factory=dict)
    extractions: dict[str, int] = field(default_factory=dict)
    created_resource_ids: set[str] = field(default_factory=set)
    extracted_hashes: dict[str, str] = field(default_factory=dict)
    extraction_bytes: int = 0
    seen_locators: set[str] = field(default_factory=set)
    planned: list[tuple[str, str, str, str, bool]] = field(default_factory=list)

    def count(self, name: str, amount: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + amount

    def warn(self, code: str, count: int = 1) -> None:
        self.warnings[code] = self.warnings.get(code, 0) + count

    def counters_dto(self) -> ScanCounters:
        """Freeze the accumulated counters into the typed result."""
        return ScanCounters(**{name: int(value) for name, value in self.counters.items()})


# -- loading and validation --------------------------------------------------


def _load_source_identity(session: Session, source_id: str) -> SourceIdentity:
    """Load and validate one source, including its operator-side permission."""
    if not source_id:
        raise SourceNotFoundError("Die Quelle existiert nicht", source_id=source_id)
    source = session.get(ContextSource, source_id)
    if source is None:
        raise SourceNotFoundError("Die Quelle existiert nicht", source_id=source_id)
    if session.get(Context, source.context_id) is None:  # pragma: no cover - FK protected
        raise ContextNotFoundError(
            "Der Context der Quelle existiert nicht", context_id=source.context_id
        )
    if source.source_type not in SCANNABLE_SOURCE_TYPES:
        raise SourceAccessError(
            "Dieser Quellentyp unterstuetzt keine Dateiaufnahme",
            code=CODE_SOURCE_TYPE_UNSUPPORTED,
            source_id=source.id,
            source_type=source.source_type,
        )
    if source.status != STATUS_ACTIVE:
        raise SourceAccessError(
            "Die Quelle ist nicht aktiv",
            code=CODE_SOURCE_INACTIVE,
            source_id=source.id,
            source_status=source.status,
        )
    return SourceIdentity(
        source_id=source.id,
        context_id=source.context_id,
        source_type=source.source_type,
        path=source.path,
        status=source.status,
        locator=canonical_locator(source.path),
    )


def _load_resource_identity(session: Session, resource_id: str) -> ResourceIdentity:
    """Load one resource row."""
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise ResourceNotFoundError("Die Ressource existiert nicht", resource_id=resource_id)
    return ResourceIdentity(
        resource_id=resource.id,
        context_id=resource.context_id,
        source_id=resource.source_id,
        locator=resource.locator,
        source_path_snapshot=resource.source_path_snapshot,
        relative_path=resource.relative_path,
        title=resource.title,
        resource_type=resource.resource_type,
        status=resource.status,
        current_content_hash=resource.current_content_hash,
    )
