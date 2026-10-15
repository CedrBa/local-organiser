"""Plan and persist registry observations without reading original files."""

from __future__ import annotations

import uuid
from typing import Sequence
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from organiser.db.models import (
    Resource,
    ResourceSnapshot,
    utc_now,
)
from organiser.domain.resources import (
    IngestionLimits,
    ResourceIngestionError,
    is_extractable,
)
from organiser.services.extraction import ExtractionOutcome, extractor_version_for
from organiser.services.source_scanner import (
    IngestionPolicy,
    ObservedFile,
    SourceTreeResult,
)
from .access import _address_within_source
from .constants import (
    EVENT_RESOURCE_MISSING,
    EVENT_RESOURCE_REFRESHED,
    EVENT_RESOURCE_REGISTERED,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_MISSING,
    _LOCATOR_QUERY_CHUNK,
)
from .identity import SourceIdentity, _ScanState
from .runs import _audit
from .snapshots import _plan_extractions, _store_snapshot


# -- persistence of one enumeration -----------------------------------------


def _existing_by_locator(
    session: Session, context_id: str, locators: Sequence[str]
) -> dict[str, Resource]:
    """Load the resources of a context for the given locators, in bounded chunks."""
    found: dict[str, Resource] = {}
    unique = list(dict.fromkeys(locators))
    for start in range(0, len(unique), _LOCATOR_QUERY_CHUNK):
        chunk = unique[start : start + _LOCATOR_QUERY_CHUNK]
        for resource in session.scalars(
            select(Resource).where(Resource.context_id == context_id, Resource.locator.in_(chunk))
        ):
            found[resource.locator] = resource
    return found


def _refresh_resource(
    resource: Resource,
    observed: ObservedFile,
    *,
    update_labels: bool = True,
    update_version: bool = True,
) -> bool:
    """Update an existing resource row from a fresh observation.

    The current version of a resource is the hash of the last *successful*
    observation, and an observation that could not read the bytes proves nothing
    about the current state of the document. So the two directions are symmetric:

    * **with** a hash, hash, byte size and mtime are written together or not at
      all -- they describe one measured version;
    * **without** a hash (too large, budget spent, unreadable, unstable, empty,
      short read) the current hash is cleared to ``None``. It is *not* kept: a kept
      hash would make the caller fall back to an older text as if it were the
      current state of a document nobody could read. ``None`` is the honest answer
      and it is the contract the rest of the system already understands
      (``no_snapshot_for_current_hash``).

    Nothing is deleted either way. The immutable ``resource_snapshots`` keep the
    hash of the version that was really read, so the history stays complete and
    ``byte_size``/``mtime_ns`` keep describing the last version that was measured
    together with its hash. Only the *currency* of the resource is withdrawn.

    ``update_labels`` is false for an observation that comes from a source which
    does not own this resource: a label is derived from the owning source's scope,
    so a neighbouring source must not rename the row. Returns whether anything
    actually changed.

    ``update_version`` is false for that same foreign observation, and it gates the
    *destructive* direction only. A foreign source that successfully hashed the
    bytes does refresh the version -- the hash is a property of the file, not of
    the row's owner, and two sources that both read it measured the same bytes. But
    a foreign source that could **not** read the bytes has learned nothing about
    them: withdrawing the owner's current hash on that evidence would let a
    neighbouring source, merely by failing (its own budget, a transient EACCES, an
    empty read at that moment), delete the currency of a document that its owner
    had just measured successfully. Withdrawals are therefore gated on the owning
    source, so the version is only ever withdrawn by the source responsible for it.
    """
    changed = False
    if observed.content_hash is None:
        if update_version and resource.current_content_hash is not None:
            resource.current_content_hash = None
            changed = True
    else:
        if resource.current_content_hash != observed.content_hash:
            resource.current_content_hash = observed.content_hash
            changed = True
        if resource.byte_size != observed.byte_size:
            resource.byte_size = observed.byte_size
            changed = True
        if resource.mtime_ns != observed.mtime_ns:
            resource.mtime_ns = observed.mtime_ns
            changed = True
    if update_labels:
        if resource.title != observed.title:
            resource.title = observed.title
            changed = True
        if resource.relative_path != observed.relative_path:
            resource.relative_path = observed.relative_path
            changed = True
        if resource.resource_type != observed.resource_type:
            resource.resource_type = observed.resource_type
            changed = True
    if resource.status == STATUS_MISSING:
        resource.status = STATUS_ACTIVE
        changed = True
    # ``last_seen_at`` means "the owning source saw this address in this run". It
    # is deliberately *not* a successful-read timestamp: a file that was seen and
    # could not be read gets it too.
    resource.last_seen_at = utc_now()
    return changed


def _rebind_orphan_resource(
    resource: Resource,
    observed: ObservedFile,
    source: SourceIdentity,
    *,
    policy: IngestionPolicy,
) -> str | None:
    """Attach a resource that lost its source back to this source, or say why not.

    ``resources.source_id`` is ``ON DELETE SET NULL``: deleting a source leaves its
    resources behind without an owner. Such a row can never be extracted, never be
    verified and never become ``missing``, because every one of those decisions asks
    for an owning source. A scan that *observes* the address again is the one place
    that can fix this, because it is a fresh, authorized observation of the very
    same file.

    The re-adoption is therefore deliberate and conservative:

    * only a row whose ``source_id`` is ``NULL`` is touched -- a row owned by a
      different, still valid source is never taken over (that stays a duplicate);
    * the address must still be the source itself or below it, and it is authorised
      against the operator policy again *inside the write transaction*, next to the
      commit;
    * an archived row is left alone: archiving is an explicit decision, and silently
      re-owning it would also silently un-archive it.

    Returns ``"rebound"``, the reason it was refused (``"archived"``,
    ``"address_outside_source"``, ``"policy_denied"``) or ``None`` when there was
    nothing to do.
    """
    if resource.source_id is not None:
        return None
    if resource.status == STATUS_ARCHIVED:
        return "archived"
    if not _address_within_source(observed.locator, source.locator):
        return "address_outside_source"
    try:
        policy.authorize(observed.locator, expect="file")
    except ResourceIngestionError:
        return "policy_denied"
    resource.source_id = source.source_id
    resource.source_path_snapshot = source.path
    return "rebound"


def _observation_payload(
    resource: Resource,
    observed: ObservedFile,
    source: SourceIdentity,
    *,
    rebound: bool = False,
    version_invalidated: bool = False,
) -> dict:
    payload = {
        "context_id": resource.context_id,
        "source_id": source.source_id,
        "relative_path": resource.relative_path,
        "resource_type": resource.resource_type,
        "byte_size": resource.byte_size,
        "has_content_hash": resource.current_content_hash is not None,
        "unread_reason": observed.unread_reason,
    }
    if rebound:
        payload["rebound_source"] = True
    if version_invalidated:
        # The currency of the row was withdrawn, not its history: the reason the
        # bytes could not be read is right next to it.
        payload["version_invalidated"] = True
    return payload


def _plan_observations(
    session: Session,
    source: SourceIdentity,
    tree: SourceTreeResult,
    *,
    limits: IngestionLimits,
) -> tuple[_ScanState, list[tuple[str, str, str, str]]]:
    """Decide what one enumeration result means, without writing anything.

    Runs in a read transaction that the caller closes immediately afterwards.
    Returns the counters/warnings collected so far plus the candidates for
    extraction, so the expensive phase (reading and parsing documents) can happen
    with no transaction open at all.
    """
    state = _ScanState()
    existing = _existing_by_locator(
        session, source.context_id, [item.locator for item in tree.files]
    )
    seen: set[str] = set()
    candidates: list[tuple[str, str, str, str]] = []
    planned: list[tuple[str, str, str, str, bool]] = []  # locator, type, hash, id, created
    for observed in tree.files:
        seen.add(observed.locator)
        resource = existing.get(observed.locator)
        created = resource is None
        duplicate = resource is not None and resource.source_id not in (source.source_id, None)
        if duplicate:
            state.count("duplicates")
        state.count("observed")
        if observed.unread_reason:
            state.warn(observed.unread_reason)
        resource_id = resource.id if resource is not None else ""
        planned.append(
            (
                observed.locator,
                observed.resource_type,
                observed.content_hash or "",
                resource_id,
                created,
            )
        )
        if (
            observed.content_hash is not None
            and is_extractable(observed.resource_type)
            and extractor_version_for(observed.resource_type) is not None
        ):
            candidates.append(
                (observed.locator, observed.resource_type, observed.content_hash, resource_id)
            )
    state.count("skipped_unsafe", tree.skipped_unsafe)
    state.count("skipped_excluded", tree.skipped_excluded)
    state.count("entries", tree.entries_seen)
    pending = _plan_extractions(session, candidates, limits=limits, state=state)

    if not tree.complete:
        state.warn("scan_incomplete_no_missing_decision")
    state.seen_locators = seen
    state.planned = planned
    return state, pending


def _apply_observations(
    session: Session,
    source: SourceIdentity,
    tree: SourceTreeResult,
    state: _ScanState,
    extractions: dict[str, tuple[str, ExtractionOutcome]],
    *,
    policy: IngestionPolicy,
) -> _ScanState:
    """Write the planned result inside the caller's write transaction.

    No file is read here. The observations were collected before the transaction
    was opened, and the extraction outcomes are handed in, so SQLite's write lock
    is held for the duration of a few statements only.
    """
    for locator, resource_type, content_hash, resource_id, created in state.planned:
        observed = next(item for item in tree.files if item.locator == locator)
        resource = session.get(Resource, resource_id) if resource_id else None
        rebound = False
        version_invalidated = False
        if resource is None:
            resource = Resource(
                id=str(uuid.uuid4()),
                context_id=source.context_id,
                source_id=source.source_id,
                locator=observed.locator,
                relative_path=observed.relative_path,
                source_path_snapshot=source.path,
                title=observed.title,
                resource_type=observed.resource_type,
                status=STATUS_ACTIVE,
                current_content_hash=observed.content_hash,
                byte_size=observed.byte_size,
                mtime_ns=observed.mtime_ns,
                last_seen_at=utc_now(),
            )
            session.add(resource)
            state.created_resource_ids.add(resource.id)
        else:
            # A resource that lost its source gets its owner back here, while the
            # address is authorised again and the source identity is already
            # re-checked by the caller.
            outcome = _rebind_orphan_resource(resource, observed, source, policy=policy)
            if outcome == "rebound":
                state.count("rebound")
                rebound = True
            elif outcome is not None:
                state.warn(f"rebind_skipped_{outcome}")
            # A label belongs to the owning source's scope. An observation from a
            # source that does not own the row refreshes the version but never
            # renames it; a fresh owner (the rebind above) recomputes it once,
            # from its own scope.
            #
            # Ownership also gates the *withdrawal* of the version: a neighbouring
            # source that failed to read the bytes must not retire the current hash
            # the owner had just measured. Only the owner can invalidate.
            hash_before = resource.current_content_hash
            owns_resource = rebound or resource.source_id == source.source_id
            changed = _refresh_resource(
                resource, observed, update_labels=owns_resource, update_version=owns_resource
            )
            version_invalidated = hash_before is not None and resource.current_content_hash is None
            if changed or rebound:
                state.count("refreshed")
            else:
                state.count("unchanged")
        if created:
            state.count("created")
        _audit(
            session,
            event_type=EVENT_RESOURCE_REGISTERED if created else EVENT_RESOURCE_REFRESHED,
            entity_type="resource",
            entity_id=resource.id,
            payload=_observation_payload(
                resource, observed, source, rebound=rebound, version_invalidated=version_invalidated
            ),
            source_id=source.source_id,
        )
    session.flush()
    if tree.complete:
        _mark_missing(session, source, state)
    else:
        _mark_nothing_missing(session, source, state)
    session.flush()
    for locator, _resource_type, _hash, _id, _created in state.planned:
        extracted = extractions.get(locator)
        if extracted is None:
            continue
        content_hash, outcome = extracted
        resource = session.scalar(
            select(Resource).where(
                Resource.context_id == source.context_id, Resource.locator == locator
            )
        )
        if resource is None:  # pragma: no cover - planned and applied in one transaction
            continue
        # The planned version was the one the enumeration saw; the extracted bytes
        # are the ones that are stored. A pair that already exists (the file
        # reverted between the two reads, or a concurrent extraction) is reused
        # rather than inserted twice.
        existing = session.scalar(
            select(ResourceSnapshot.id).where(
                ResourceSnapshot.resource_id == resource.id,
                ResourceSnapshot.content_hash == content_hash,
                ResourceSnapshot.extractor_version == outcome.extractor_version,
            )
        )
        if existing is not None:
            state.extractions["reused"] = state.extractions.get("reused", 0) + 1
            continue
        _store_snapshot(session, resource, content_hash, outcome, source=source, state=state)
    return state


def _mark_missing(session: Session, source: SourceIdentity, state: _ScanState) -> None:
    """Set ``missing`` on the resources of this source that a full scan did not see.

    Only reached for a complete enumeration. The update is set based so the
    session never has to hold every resource of a heavily scanned source.
    """
    seen = state.seen_locators
    # Only the rows that actually transition are materialised, and they are
    # fetched *before* the update so that resources which were already missing do
    # not produce a second RESOURCE_MISSING event on every later scan.
    affected = list(
        session.execute(
            select(Resource.id, Resource.relative_path).where(
                Resource.context_id == source.context_id,
                Resource.source_id == source.source_id,
                Resource.status == STATUS_ACTIVE,
                Resource.locator.notin_(seen) if seen else Resource.locator.is_not(None),
            )
        )
    )
    if not affected:
        return
    session.execute(
        update(Resource)
        .where(Resource.id.in_([row[0] for row in affected]))
        .values(status=STATUS_MISSING, updated_at=utc_now())
    )
    state.count("marked_missing", len(affected))
    for resource_id, relative_path in affected:
        _audit(
            session,
            event_type=EVENT_RESOURCE_MISSING,
            entity_type="resource",
            entity_id=resource_id,
            payload={
                "context_id": source.context_id,
                "source_id": source.source_id,
                "relative_path": relative_path,
                "reason": "not_seen_in_complete_scan",
            },
            source_id=source.source_id,
        )


def _mark_nothing_missing(session: Session, source: SourceIdentity, state: _ScanState) -> None:
    """An incomplete scan leaves the status of every unseen resource untouched."""
    del session, source
    state.counters["marked_missing"] = 0
