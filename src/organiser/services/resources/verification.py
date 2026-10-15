"""Read-only resource version checks against fresh permissions and file bytes."""

from __future__ import annotations

from datetime import UTC, datetime
from sqlalchemy.orm import Session
from organiser.core.config import Settings
from organiser.domain.resources import (
    CODE_HASH_MISMATCH,
    DEFAULT_LIMITS,
    IngestionLimits,
    ResourceIngestionError,
    ResourceVersionCheck,
    ResourceVersionMismatchError,
    SourceStaleError,
)
from organiser.services.source_scanner import (
    FileRead,
    IngestionPolicy,
    read_local_file,
)
from .access import _authorize_resource_address, _load_source_for_access, _read_refusal_reason
from .constants import (
    REASON_FILE_UNREADABLE,
    REASON_RESOURCE_EMPTY,
    REASON_RESOURCE_HASH_MOVED,
    REASON_RESOURCE_STATUS_ARCHIVED,
    REASON_RESOURCE_STATUS_MISSING,
    STATUS_ACTIVE,
    STATUS_MISSING,
)
from .identity import ResourceIdentity, SourceIdentity, _load_resource_identity
from .transactions import _end_read_transaction, _require_application_boundary


# -- pre-apply freshness check ----------------------------------------------


def _failed_check(
    resource: ResourceIdentity | None,
    resource_id: str,
    expected_hash: str,
    checked_at: datetime,
    reason: str,
    *,
    source: SourceIdentity | None = None,
    actual_hash: str | None = None,
) -> ResourceVersionCheck:
    return ResourceVersionCheck(
        resource_id=resource_id,
        context_id=resource.context_id if resource else "",
        source_id=resource.source_id if resource else None,
        ok=False,
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        resource_status=resource.status if resource else "",
        source_status=source.status if source else None,
        checked_at=checked_at,
        reason=reason,
    )


def verify_resource_version(
    session: Session,
    resource_id: str,
    expected_hash: str,
    settings: Settings,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    policy: IngestionPolicy | None = None,
) -> ResourceVersionCheck:
    """Check permission, address and bytes of one resource without writing anything.

    This is the check E calls before applying a resource based proposal. It
    answers one question: *may a proposal that was built from ``expected_hash``
    still be applied to the document as it is right now?*

    It writes nothing at all -- no snapshot, no event, no ProcessingRun, no topic,
    no knowledge -- because it runs in the same breath as a later apply and must
    not be able to change what it is checking. Every ``ok=False`` carries a reason
    code instead of raising, because stale evidence is an ordinary outcome of a
    proposal, not an exceptional one. That includes a failing read: an unreadable
    original (including a raw ``OSError`` such as ``EIO``) is reported as
    ``ok=False`` with ``file_unreadable``, never as an untyped crash.

    The two reason codes that look alike are kept apart on purpose.
    ``source_not_allowed`` means *the operator policy does not cover this address*
    and is decided before anything is read; ``file_unreadable`` means *the policy
    covers it and the bytes could not be read anyway*. The enumeration reports the
    identical condition as ``file_unreadable``, so one file and one cause always
    carry one code. See :func:`_read_refusal_reason`.
    """
    _require_application_boundary(session)
    checked_at = datetime.now(UTC)
    # The policy is derived from operator settings and cannot change between the two
    # checks below, so it is built once and reused. Building it twice would also
    # mean the authorisation and the read could disagree about the same path.
    effective_policy = policy or IngestionPolicy.from_settings(settings)
    resource: ResourceIdentity | None = None
    source: SourceIdentity | None = None
    read: FileRead | None = None
    try:
        resource = _load_resource_identity(session, resource_id)
        try:
            source = _load_source_for_access(session, resource.source_id)
        except SourceStaleError as stale:
            return _failed_check(
                resource,
                resource_id,
                expected_hash,
                checked_at,
                str(stale.context.get("reason", stale.code)),
            )
        try:
            _authorize_resource_address(resource, source, effective_policy)
        except ResourceIngestionError as error:
            return _failed_check(
                resource, resource_id, expected_hash, checked_at, error.code, source=source
            )
        if resource.status != STATUS_ACTIVE:
            return _failed_check(
                resource,
                resource_id,
                expected_hash,
                checked_at,
                REASON_RESOURCE_STATUS_MISSING
                if resource.status == STATUS_MISSING
                else REASON_RESOURCE_STATUS_ARCHIVED,
                source=source,
            )
        # Close the read transaction before touching the filesystem: no lock is
        # held while the file is read, not even a read lock.
        _end_read_transaction(session)
        try:
            read = read_local_file(resource.locator, effective_policy, limits)
        except ResourceIngestionError as error:
            return _failed_check(
                resource,
                resource_id,
                expected_hash,
                checked_at,
                _read_refusal_reason(error),
                source=source,
            )
        except OSError:
            return _failed_check(
                resource,
                resource_id,
                expected_hash,
                checked_at,
                REASON_FILE_UNREADABLE,
                source=source,
            )
    except ResourceIngestionError as error:
        return _failed_check(
            resource, resource_id, expected_hash, checked_at, error.code, source=source
        )
    except OSError:
        return _failed_check(
            resource, resource_id, expected_hash, checked_at, REASON_FILE_UNREADABLE, source=source
        )
    finally:
        # Every exit path, including the refusals above, leaves no open transaction.
        _end_read_transaction(session)

    assert resource is not None and source is not None and read is not None
    if read.is_empty:
        # Nothing can have been built on an empty version: no snapshot carries it
        # and the registry never declares it current. Verifying it would hand a
        # caller the impression that an empty document can be applied to.
        return _failed_check(
            resource,
            resource_id,
            expected_hash,
            checked_at,
            REASON_RESOURCE_EMPTY,
            source=source,
            actual_hash=read.content_hash,
        )
    if read.content_hash != expected_hash:
        return _failed_check(
            resource,
            resource_id,
            expected_hash,
            checked_at,
            CODE_HASH_MISMATCH,
            source=source,
            actual_hash=read.content_hash,
        )
    if (
        resource.current_content_hash is not None
        and resource.current_content_hash != read.content_hash
    ):
        return _failed_check(
            resource,
            resource_id,
            expected_hash,
            checked_at,
            REASON_RESOURCE_HASH_MOVED,
            source=source,
            actual_hash=read.content_hash,
        )
    return ResourceVersionCheck(
        resource_id=resource_id,
        context_id=resource.context_id,
        source_id=resource.source_id,
        ok=True,
        expected_hash=expected_hash,
        actual_hash=read.content_hash,
        resource_status=resource.status,
        source_status=source.status,
        checked_at=checked_at,
        reason=None,
    )


def assert_resource_version(
    session: Session,
    resource_id: str,
    expected_hash: str,
    settings: Settings,
    *,
    limits: IngestionLimits = DEFAULT_LIMITS,
    policy: IngestionPolicy | None = None,
) -> ResourceVersionCheck:
    """Same check, but a refusal raises :class:`ResourceVersionMismatchError`."""
    check = verify_resource_version(
        session, resource_id, expected_hash, settings, limits=limits, policy=policy
    )
    if not check.ok:
        raise ResourceVersionMismatchError(
            "Die Ressource entspricht nicht mehr der erwarteten Version",
            resource_id=resource_id,
            reason=check.reason,
        )
    return check
