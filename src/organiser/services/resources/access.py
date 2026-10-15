"""Fresh registration, permission and resource-address checks before access."""

from __future__ import annotations

from sqlalchemy.orm import Session
from organiser.db.models import (
    ContextSource,
)
from organiser.domain.resources import (
    CODE_SOURCE_NOT_ALLOWED,
    CODE_SOURCE_UNSAFE,
    ResourceIngestionError,
    SourceStaleError,
    SourceUnsafeError,
    canonical_locator,
    is_within,
)
from organiser.services.source_scanner import (
    IngestionPolicy,
)
from .constants import (
    REASON_FILE_UNREADABLE,
    REASON_SOURCE_REMOVED,
    REASON_SOURCE_STATUS,
    SCANNABLE_SOURCE_TYPES,
    STATUS_ACTIVE,
)
from .identity import ResourceIdentity, SourceIdentity


def _load_source_for_access(session: Session, source_id: str | None) -> SourceIdentity:
    """Load the source a stored resource depends on, or declare it stale.

    Reading an existing resource is not the same as scanning a source. For a
    scan, a non-active source is a refusal with its own code. For an already
    registered resource, a source that was archived, deleted or retyped means the
    stored address is no longer backed by a permission: that is *staleness*, and
    it is reported as such.
    """
    if not source_id:
        raise SourceStaleError(
            "Die Quelle dieser Ressource existiert nicht mehr; der Zugriff ist ungueltig",
            reason=REASON_SOURCE_REMOVED,
        )
    source = session.get(ContextSource, source_id)
    if source is None:
        raise SourceStaleError(
            "Die Quelle dieser Ressource existiert nicht mehr; der Zugriff ist ungueltig",
            reason=REASON_SOURCE_REMOVED,
            source_id=source_id,
        )
    if source.source_type not in SCANNABLE_SOURCE_TYPES:
        raise SourceStaleError(
            "Die Quelle dieser Ressource ist kein unterstuetzter Quellentyp mehr",
            reason="source_type_changed",
            source_id=source_id,
            source_type=source.source_type,
        )
    if source.status != STATUS_ACTIVE:
        raise SourceStaleError(
            "Die Quelle ist nicht mehr aktiv", reason=REASON_SOURCE_STATUS, source_id=source_id
        )
    return SourceIdentity(
        source_id=source.id,
        context_id=source.context_id,
        source_type=source.source_type,
        path=source.path,
        status=source.status,
        locator=canonical_locator(source.path),
    )


def _authorize_resource_address(
    resource: ResourceIdentity, source: SourceIdentity, policy: IngestionPolicy
) -> None:
    """Prove that reading this resource is allowed right now.

    Three independent things must hold: the source is still the same and still
    active, the address is still covered by the operator allowlist, and the
    address is still inside that source. The address snapshot stored at
    registration time is not evidence for any of them.
    """
    if resource.source_id is None:
        raise SourceStaleError(
            "Die Quelle dieser Ressource existiert nicht mehr; der Zugriff ist ungueltig",
            reason=REASON_SOURCE_REMOVED,
            resource_id=resource.resource_id,
        )
    if resource.source_id != source.source_id:
        raise SourceStaleError(
            "Die Ressource gehoert zu einer anderen Quelle",
            reason="source_mismatch",
            resource_id=resource.resource_id,
        )
    if source.status != STATUS_ACTIVE:
        raise SourceStaleError(
            "Die Quelle ist nicht mehr aktiv",
            reason=REASON_SOURCE_STATUS,
            source_id=source.source_id,
        )
    policy.authorize(resource.locator, expect="file")
    if not _address_within_source(resource.locator, source.locator):
        raise SourceStaleError(
            "Die Adresse der Ressource liegt nicht mehr unter der Quelle",
            reason="address_outside_source",
            resource_id=resource.resource_id,
        )


def _address_within_source(locator: str, source_locator: str) -> bool:
    """True if ``locator`` is the source address itself or below it.

    A ``document`` or ``note`` source *is* its file, so its resource carries exactly
    the source address. Requiring a strict prefix would make every extraction and
    every freshness check of such a resource fail as "outside the source".
    """
    return locator == source_locator or is_within(source_locator, locator)


def _os_error_to_refusal(error: OSError) -> ResourceIngestionError:
    """Translate an unexpected OS error into a coded refusal.

    Without this, a failing ``read`` or ``fstat`` would escape as a raw OSError:
    the caller's session would keep an open transaction and the ProcessingRun
    would stay ``running`` forever.
    """
    return SourceUnsafeError("Datei ist nicht lesbar", code=CODE_SOURCE_UNSAFE, reason="unreadable")


def _read_refusal_reason(error: ResourceIngestionError | OSError) -> str:
    """Name a *read* refusal the way the enumeration names it.

    ``read_local_file`` translates ``EACCES``/``EPERM`` into
    ``source_not_allowed``. That is the right answer for a *source* the operator
    cannot open, and the wrong answer here: the address was authorised before the
    read, so the operator policy is not in question and a caller reading
    ``source_not_allowed`` would look for a changed allowlist that never changed.
    The enumeration makes exactly the same distinction for exactly the same file --
    an unreadable file inside an allowed root is ``file_unreadable``, never "not
    allowed" -- so the two readers of one cause must not use two codes.

    Every other refusal keeps its own code: a link is still ``source_unsafe``, a
    file that changed while it was read is still ``file_changed_during_read``, and
    a genuine allowlist revocation never reaches this function at all, because the
    caller authorises the address before the read and refuses it there.

    A raw ``OSError`` (``EIO`` on a failing disk, ``ESTALE`` on NFS) carries no code
    of its own and means the same thing this whole function is about: the policy
    covers the address and the bytes could not be read. It is one mapping, shared
    by the extraction read of a scan and by the freshness check, so the two can
    never drift apart again.
    """
    if isinstance(error, OSError):
        return REASON_FILE_UNREADABLE
    if error.code == CODE_SOURCE_NOT_ALLOWED and str(error.context.get("reason")) == "permission":
        return REASON_FILE_UNREADABLE
    return error.code
