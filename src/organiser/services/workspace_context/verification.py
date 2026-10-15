"""Recheck a manifest read-only without trusting stale ORM identity-map values."""

from __future__ import annotations

from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
    ResourceSnapshot,
)
from organiser.domain.workspace_contracts import (
    WorkspaceManifest,
)
from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
    KnowledgeError,
    KnowledgeStore,
)
from .policy import DEFAULT_WORKSPACE_POLICY, WorkspacePolicy
from .redaction import _INVALID_KNOWLEDGE_PATH_MARKER, _is_safe_knowledge_path
from .values import _stamp, _stamp_key
from .verification_contracts import (
    _MANIFEST_COLUMNS,
    _MANIFEST_FIELDS,
    _MANIFEST_MODELS,
    _SNAPSHOT_MANIFEST_COLUMNS,
)
from .verification_entities import _actual_field, _verify_event
from .verification_knowledge import _knowledge_address_reason
from .verification_results import WorkspaceManifestChange, WorkspaceManifestVerification


def verify_workspace_manifest(
    session: Session,
    store: KnowledgeStore | None,
    manifest: WorkspaceManifest,
    policy: WorkspacePolicy | None = None,
) -> WorkspaceManifestVerification:
    """Re-check a recorded manifest against the current state, without writing.

    This is the precondition the propose/apply component needs: a packet is stale
    if any used entity changed status or ``updated_at``, if a used snapshot was
    replaced or removed, or if a knowledge file was edited or removed by hand. A
    markdown edit produces no event, which is precisely why the knowledge hashes are
    compared directly instead of relying on an event id.

    Nothing is written, nothing is created and no conflict is repaired here. A caller
    that wants to apply must regenerate the packet explicitly.

    ``policy`` must be the policy the recorded packet was built with. The comparison
    truncates with the same bounds, otherwise a title longer than ``max_title_chars``
    could never verify: the manifest holds the bounded value, so comparing it against
    the raw column would report a change for untouched data and close the apply gate
    permanently.
    """
    policy = (policy or DEFAULT_WORKSPACE_POLICY).clamped()
    changes: list[WorkspaceManifestChange] = []
    manifest_context = next(
        (entry for entry in manifest.entities if entry.entity_type == "context"), None
    )
    manifest_context_id = manifest_context.entity_id if manifest_context is not None else ""
    # The two knowledge bindings a recorded address is checked against: the one the
    # manifest claims for its own context, and the one the database holds *now*.
    # Both are needed; see :func:`_knowledge_address_reason`.
    recorded_path = (
        manifest_context.selected_fields.get("knowledge_path")
        if manifest_context is not None
        else None
    )
    current_path: Any = None
    with session.no_autoflush:
        if manifest_context_id:
            current_path = session.scalar(
                select(Context.knowledge_path).where(Context.id == manifest_context_id)
            )
        for entry in manifest.entities:
            if entry.entity_type == "event":
                changes.extend(_verify_event(session, entry, policy))
                continue
            model = _MANIFEST_MODELS.get(entry.entity_type)
            if model is None:  # pragma: no cover - A fixed the entity vocabulary
                changes.append(
                    WorkspaceManifestChange(
                        "entity", entry.entity_id, "entity_type", entry.entity_type, "unknown"
                    )
                )
                continue
            reference = f"{entry.entity_type}:{entry.entity_id}"
            cols = _MANIFEST_COLUMNS[entry.entity_type]
            row = session.execute(select(*cols).where(model.id == entry.entity_id)).first()
            if row is None:
                changes.append(
                    WorkspaceManifestChange("missing", reference, "row", "present", "absent")
                )
                continue
            current_status = getattr(row, "status", None) or None
            if (entry.status or None) != current_status:
                changes.append(
                    WorkspaceManifestChange(
                        "entity", reference, "status", entry.status or "", current_status or ""
                    )
                )
            current_updated = _stamp(getattr(row, "updated_at", None))
            if _stamp_key(entry.updated_at) != _stamp_key(current_updated):
                changes.append(
                    WorkspaceManifestChange(
                        "entity",
                        reference,
                        "updated_at",
                        _stamp_key(entry.updated_at),
                        _stamp_key(current_updated),
                    )
                )
            allowed = _MANIFEST_FIELDS[entry.entity_type]
            for name, expected in entry.selected_fields.items():
                if name not in allowed:
                    changes.append(
                        WorkspaceManifestChange(
                            "manifest", reference, name, str(expected), "unverifiable"
                        )
                    )
                    continue
                actual = _actual_field(session, row, name, policy, entity_type=entry.entity_type)
                if name == "knowledge_path" and (
                    expected == _INVALID_KNOWLEDGE_PATH_MARKER
                    or actual == _INVALID_KNOWLEDGE_PATH_MARKER
                    or (actual is not None and not _is_safe_knowledge_path(actual))
                ):
                    changes.append(
                        WorkspaceManifestChange(
                            "entity",
                            reference,
                            "knowledge_path",
                            str(expected),
                            "invalid_knowledge_path",
                        )
                    )
                    continue
                if actual != expected:
                    changes.append(
                        WorkspaceManifestChange(
                            "entity", reference, name, str(expected), str(actual)
                        )
                    )
        for entry in manifest.snapshots:
            reference = f"snapshot:{entry.snapshot_id}"
            row = session.execute(
                select(*_SNAPSHOT_MANIFEST_COLUMNS).where(ResourceSnapshot.id == entry.snapshot_id)
            ).first()
            if row is None:
                changes.append(
                    WorkspaceManifestChange("missing", reference, "row", "present", "absent")
                )
                continue
            if row.content_hash != entry.content_hash:
                changes.append(
                    WorkspaceManifestChange(
                        "snapshot", reference, "content_hash", entry.content_hash, row.content_hash
                    )
                )
            if row.extractor_version != entry.extractor_version:
                changes.append(
                    WorkspaceManifestChange(
                        "snapshot",
                        reference,
                        "extractor_version",
                        entry.extractor_version,
                        row.extractor_version,
                    )
                )
            row_status = row.status if isinstance(row.status, str) else row.status.value
            entry_status = (
                entry.status.value if hasattr(entry.status, "value") else str(entry.status)
            )
            if row_status != entry_status:
                changes.append(
                    WorkspaceManifestChange(
                        "snapshot", reference, "status", entry_status, row_status
                    )
                )
        for entry in manifest.knowledge:
            if store is None:
                changes.append(
                    WorkspaceManifestChange(
                        "knowledge", entry.relative_path, "reader", "store", "none"
                    )
                )
                continue
            if entry.context_id != manifest_context_id:
                # The manifest's own context entity is the anchor. Trusting the entry's
                # context_id would make the comparison a SHA-256 oracle over any other
                # context's markdown, since both values come from the same untrusted record.
                changes.append(
                    WorkspaceManifestChange(
                        "knowledge",
                        entry.relative_path,
                        "context_id",
                        entry.context_id,
                        manifest_context_id,
                    )
                )
                continue
            reason = _knowledge_address_reason(entry.relative_path, recorded_path, current_path)
            if reason is not None:
                # The refused address is already the ``reference``; echoing it a second time
                # as the *expected* value would only add an untrusted string to the display
                # fields without adding information.
                changes.append(
                    WorkspaceManifestChange(
                        "knowledge", entry.relative_path, "knowledge_path", "bound", reason
                    )
                )
                continue
            try:
                read = store.read_excerpt(entry.relative_path, 1, MAX_EXCERPT_BYTES)
            except KnowledgeError as error:
                changes.append(
                    WorkspaceManifestChange(
                        "knowledge", entry.relative_path, "readable", "file", type(error).__name__
                    )
                )
                continue
            if read.content_hash != entry.content_hash:
                changes.append(
                    WorkspaceManifestChange(
                        "knowledge",
                        entry.relative_path,
                        "content_hash",
                        entry.content_hash,
                        read.content_hash,
                    )
                )
    return WorkspaceManifestVerification(
        unchanged=not changes,
        changes=tuple(changes),
        checked_entities=len(manifest.entities),
        checked_snapshots=len(manifest.snapshots),
        checked_knowledge_files=len(manifest.knowledge),
    )
