"""Verify entity, link and event projections against fresh database values."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    ContextSource,
    Resource,
    ResourceSnapshot,
)
from organiser.domain.workspace_contracts import (
    EntityManifestEntry,
)
from .events import _load_event_row
from .policy import WorkspacePolicy
from .redaction import _safe_knowledge_path, _source_label, _truncate
from .values import _digest_json, _stamp_key
from .verification_contracts import (
    _BOUND_TEXT_FIELDS,
    _EVENT_COLUMN_FIELDS,
    _LINK_TABLES,
    _MANIFEST_FIELDS,
)
from .verification_results import WorkspaceManifestChange


def _event_payload_bound(entry: EntityManifestEntry, policy: WorkspacePolicy) -> int:
    """How many payload characters this verification may read for one event.

    The packet recorded the cap it used, and the caller supplies the policy it verifies
    with. Neither may raise the other's limit, so the effective bound is the smaller of
    the two, and the policy is already ``clamped()`` -- which means a hand-crafted
    ``payload_read_cap`` of ten gigabytes buys nothing.

    Taking the *minimum* is what makes a legitimately narrower recorded cap work: a
    packet built with a small cap hashed only what fitted under it, and a verification
    with the default cap must reproduce exactly that decision rather than load more and
    report a difference that is not one.

    This field is deliberately an **input** to the verification and not a value that is
    compared: there is no authoritative "correct" cap other than the caller's policy, and
    comparing the two would report a change for every legitimate policy difference -- the
    very false stale this component already had to undo once. It cannot launder
    anything: it only ever *reduces* what is read, an unknown key still fails as
    ``unverifiable``, and a recorded hash the verifier may not reproduce is reported
    explicitly instead of being blanked.
    """
    try:
        recorded = int(entry.selected_fields.get("payload_read_cap"))
    except (TypeError, ValueError):
        recorded = policy.max_event_payload_bytes
    return min(max(0, recorded), policy.max_event_payload_bytes)


def _verify_event(
    session: Session, entry: EntityManifestEntry, policy: WorkspacePolicy
) -> list[WorkspaceManifestChange]:
    """Compare only allowed bounded event fields against a fresh database projection."""
    reference = f"event:{entry.entity_id}"
    try:
        event_id = int(entry.entity_id)
    except (TypeError, ValueError):  # pragma: no cover - event ids are integers
        return [WorkspaceManifestChange("event", reference, "id", entry.entity_id, "invalid")]
    row = _load_event_row(session, event_id, _event_payload_bound(entry, policy))
    if row is None:
        return [WorkspaceManifestChange("missing", reference, "row", "present", "absent")]
    changes: list[WorkspaceManifestChange] = []
    for name, expected in entry.selected_fields.items():
        if name == "payload_read_cap":
            # Read as an input above, never compared. Documented in
            # ``_event_payload_bound``.
            continue
        if name not in _MANIFEST_FIELDS["event"]:
            changes.append(
                WorkspaceManifestChange("manifest", reference, name, str(expected), "unverifiable")
            )
            continue
        if name == "payload_hash":
            if not expected:
                # The recorded packet measured this payload but never loaded it, so it
                # used nothing beyond the length -- which ``payload_length`` compares.
                continue
            if not row.payload_loaded:
                # The recorded packet hashed this payload under a looser cap than this
                # verification is allowed to use. Reporting a content difference we may
                # not read would be a lie, and silently passing would be a hole; the
                # honest answer is a visible, fail-closed policy mismatch.
                changes.append(
                    WorkspaceManifestChange(
                        "event", reference, name, str(expected), "payload_beyond_verifier_cap"
                    )
                )
                continue
        if name in _EVENT_COLUMN_FIELDS:
            actual = _actual_field(session, row, name, policy, entity_type="event")
        elif name == "payload_length":
            actual = row.payload_length
        elif name == "payload_hash":
            actual = _digest_json(row.payload)
        else:  # pragma: no cover - the whitelist above is exhaustive
            actual = None
        if actual != expected:
            changes.append(
                WorkspaceManifestChange("event", reference, name, str(expected), str(actual))
            )
    return changes


def _link_ids(
    session: Session, entity_id: str, entity_type: str, field: str, policy: WorkspacePolicy
) -> list[str] | None:
    """Current values of a link list, read from the link table with the recorded cap.

    The cap comes from the *policy*, not from the module default. A caller that
    recorded a manifest with ``max_ids_per_item = 2`` would otherwise have its list
    compared against five IDs, so the packet it wrote could never verify itself and a
    perfectly legitimate proposal was permanently stale.
    """
    columns = _LINK_TABLES.get((entity_type, field))
    if columns is None:
        return None
    owner_column, value_column = columns
    cap = policy.max_ids_per_item
    values = list(
        session.scalars(
            select(value_column)
            .where(owner_column == entity_id)
            .order_by(value_column)
            .limit(cap + 1)
        )
    )
    return values[:cap]


def _actual_field(
    session: Session, row: Any, name: str, policy: WorkspacePolicy, entity_type: str = ""
) -> Any:
    """Current value of a manifest field, in the bounded form the manifest recorded it.

    Truncated fields are cut with the packet's own limits, because the manifest stores
    the cut value. Comparing that against a raw column would report a change for
    untouched data and leave the apply gate permanently closed for any entity with a
    long title.
    """
    kind = entity_type or getattr(row, "entity_type", type(row).__name__.lower())
    if name in {"due_at", "earliest_start_at", "last_seen_at", "occurred_at", "created_at"}:
        return _stamp_key(getattr(row, name, None))
    if name in {"resource_ids", "topic_ids", "task_ids"}:
        row_id = getattr(row, "id", None)
        linked = _link_ids(session, row_id, kind, name, policy) if row_id is not None else None
        return linked if linked is not None else list(getattr(row, name, None) or [])
    if name == "label":
        # A derived field, not a column: the packet shows the last path segment of a
        # registered source. Re-deriving it is what makes a renamed source detectable
        # while the absolute path itself stays out of the manifest.
        if kind == "context_source" or isinstance(row, ContextSource):
            return _truncate(_source_label(row), policy.max_label_chars)[0]
        return getattr(row, name, None)
    if name == "snapshot_truncated":
        # Also not a column: it belongs to the newest snapshot of this resource, which
        # the manifest records separately.
        if kind == "resource" or isinstance(row, Resource):
            row_id = getattr(row, "id", None)
            return session.scalar(
                select(ResourceSnapshot.truncated)
                .where(ResourceSnapshot.resource_id == row_id)
                .order_by(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc())
                .limit(1)
            )
        return getattr(row, name, None)
    if name == "knowledge_path":
        return _safe_knowledge_path(getattr(row, name, None))
    value = getattr(row, name, None)
    bound = _BOUND_TEXT_FIELDS.get(name)
    if bound and isinstance(value, str):
        return _truncate(value, getattr(policy, bound))[0]
    if isinstance(value, datetime):
        return _stamp_key(value)
    return value
