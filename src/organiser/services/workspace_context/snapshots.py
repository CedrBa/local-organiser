"""Classify current evidence and load only selected snapshot excerpts."""

from __future__ import annotations

from typing import Sequence
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased, load_only
from organiser.db.models import (
    ResourceSnapshot,
)
from organiser.domain.context import (
    KNOWLEDGE_CURRENT,
    KNOWLEDGE_NO_CURRENT_HASH,
    KNOWLEDGE_RESOURCE_ARCHIVED,
    KNOWLEDGE_RESOURCE_MISSING,
    KNOWLEDGE_SNAPSHOT_FAILED,
    KNOWLEDGE_SNAPSHOT_NOT_READY,
    KNOWLEDGE_SNAPSHOT_UNSUPPORTED,
    KNOWLEDGE_SOURCE_INACTIVE,
)
from organiser.services.knowledge import (
    KnowledgeError,
    KnowledgeStore,
)
from .constants import _SNAPSHOT_META_COLUMNS
from .policy import WorkspacePolicy
from .redaction import _truncate
from .state import _ExcerptCandidate, _Prepared
from .values import _instant_key


def _load_snapshots(
    session: Session, prepared: _Prepared, resource_ids: Sequence[str], policy: WorkspacePolicy
) -> None:
    """Read bounded snapshot *metadata* for the selected resources, without text.

    Two maps come out of it: the newest snapshot overall (for state reporting) and
    the newest snapshot whose content hash equals the resource's
    ``current_content_hash`` (the only text that may be shown as current). A
    resource with more snapshots than ``max_snapshots_per_resource`` degrades visibly
    to "no current hash" instead of silently showing the wrong extract.
    """
    if not resource_ids:
        return
    per_resource = policy.max_snapshots_per_resource
    # A global ``LIMIT`` over rows ordered by resource_id lets one resource with many
    # snapshots push every other resource's evidence out of the window, so the bound
    # is expressed per resource with a window function instead. One query, exactly
    # ``per_resource`` rows per resource, no starvation.
    ranked = (
        select(
            *_SNAPSHOT_META_COLUMNS,
            func.row_number()
            .over(
                partition_by=ResourceSnapshot.resource_id,
                order_by=(ResourceSnapshot.created_at.desc(), ResourceSnapshot.id.desc()),
            )
            .label("rank"),
        )
        .where(ResourceSnapshot.resource_id.in_(resource_ids))
        .subquery()
    )
    windowed = aliased(ResourceSnapshot, ranked)
    rows = list(
        session.scalars(
            select(windowed)
            .options(
                load_only(*(getattr(windowed, column.key) for column in _SNAPSHOT_META_COLUMNS))
            )
            .where(ranked.c.rank <= per_resource)
            .order_by(ranked.c.rank)
        )
    )
    totals = {
        resource_id: int(count or 0)
        for resource_id, count in session.execute(
            select(ResourceSnapshot.resource_id, func.count())
            .where(ResourceSnapshot.resource_id.in_(resource_ids))
            .group_by(ResourceSnapshot.resource_id)
        ).all()
    }
    hashes = {resource.id: resource.current_content_hash for resource in prepared.resources}
    current_ready: list[ResourceSnapshot] = []
    for snapshot in rows:
        resource_id = snapshot.resource_id
        prepared.snapshot_by_id[snapshot.id] = snapshot
        prepared.latest_snapshot.setdefault(resource_id, snapshot)
        expected = hashes.get(resource_id)
        if expected and snapshot.content_hash == expected:
            current = prepared.current_snapshot.get(resource_id)
            # Both sides go through ``_instant_key``. The session factory runs with
            # ``expire_on_commit=False``, so a snapshot inserted through *this* session
            # still carries the aware value the Python default produced, while a
            # snapshot that came from the database carries a naive one. Two extractor
            # versions of the same content hash are enough to compare them, and the raw
            # comparison raised ``TypeError`` -- an HTTP 500 on ordinary data.
            if current is None or (_instant_key(snapshot.created_at), snapshot.id) > (
                _instant_key(current.created_at),
                current.id,
            ):
                prepared.current_snapshot[resource_id] = snapshot
            if snapshot.status == "ready":
                current_ready.append(snapshot)
    for resource_id, total in sorted(totals.items()):
        if total > per_resource:
            prepared.warnings.append(
                f"resource {resource_id}: nur die {per_resource} neuesten von {total} Extraktionen wurden "
                "geprueft; aeltere Hashes bleiben unsichtbar"
            )
    text_lengths: dict[str, int] = {}
    if current_ready:
        for snapshot_id, length in session.execute(
            select(ResourceSnapshot.id, func.length(ResourceSnapshot.text)).where(
                ResourceSnapshot.id.in_([snapshot.id for snapshot in current_ready])
            )
        ).all():
            text_lengths[snapshot_id] = int(length or 0)
    prepared.snapshot_text_lengths = text_lengths


# ---------------------------------------------------------------------------
# Resource classification and excerpt candidates
# ---------------------------------------------------------------------------


def _classify_resources(prepared: _Prepared, policy: WorkspacePolicy) -> None:
    """Decide, per resource, whether its extract may be shown as current.

    A resource contributes text only when it is active, its source is active, a
    current content hash is known and a ``ready`` snapshot exists for exactly that
    hash. Every other case becomes an explicit ``knowledge_status`` and, where it
    could mislead, a warning. Older extract text is never presented as current.
    """
    priority = 0
    for resource in prepared.resources:
        source = prepared.referenced_sources.get(resource.source_id) if resource.source_id else None
        source_unusable = resource.source_id is not None and source is None
        snapshot = prepared.current_snapshot.get(resource.id)
        status = KNOWLEDGE_CURRENT
        if resource.status == "archived":
            status = KNOWLEDGE_RESOURCE_ARCHIVED
            prepared.warnings.append(
                f"resource {resource.id}: Status archived; ein vorhandener Extrakt wird nicht als "
                "aktueller Stand verwendet"
            )
        elif resource.status != "active":
            status = KNOWLEDGE_RESOURCE_MISSING
            prepared.warnings.append(
                f"resource {resource.id}: Status {resource.status}; ein vorhandener Extrakt wird nicht "
                "als aktueller Stand verwendet"
            )
        elif source_unusable or (source is not None and source.status != "active"):
            # ``source_unusable`` covers a ``source_id`` that resolves to nothing in
            # this context -- a deleted source, or one belonging to a different context,
            # which the schema does not forbid. Treating that as "no source at all"
            # would silently grant freshness; the safe reading is "no usable source".
            status = KNOWLEDGE_SOURCE_INACTIVE
            reason = "nicht aufloesbar" if source_unusable else "nicht aktiv"
            prepared.warnings.append(
                f"resource {resource.id}: die zugehoerige Quelle ist {reason}; kein aktueller Text belegbar"
            )
        elif not resource.current_content_hash:
            status = KNOWLEDGE_NO_CURRENT_HASH
            prepared.warnings.append(
                f"resource {resource.id}: es ist keine aktuelle Inhaltsversion beobachtet; "
                "es wird kein Text behauptet"
            )
        elif snapshot is None:
            status = KNOWLEDGE_NO_CURRENT_HASH
            prepared.warnings.append(
                f"resource {resource.id}: keine Extraktion fuer den aktuellen Hash im Lesefenster"
            )
        elif snapshot.status == "unsupported":
            status = KNOWLEDGE_SNAPSHOT_UNSUPPORTED
            prepared.warnings.append(
                f"resource {resource.id}: die Extraktion wird von diesem Format nicht unterstuetzt; "
                "es ist kein Text belegbar"
            )
        elif snapshot.status == "failed":
            status = KNOWLEDGE_SNAPSHOT_FAILED
            prepared.warnings.append(
                f"resource {resource.id}: die Extraktion ist fehlgeschlagen; es ist kein aktueller Text belegbar"
            )
        elif snapshot.status != "ready":
            status = KNOWLEDGE_SNAPSHOT_NOT_READY
            prepared.warnings.append(
                f"resource {resource.id}: der Snapshot ist noch nicht bereit ({snapshot.status})"
            )
        elif not prepared.snapshot_text_lengths.get(snapshot.id):
            status = KNOWLEDGE_SNAPSHOT_NOT_READY
            prepared.warnings.append(
                f"resource {resource.id}: der Snapshot ist bereit, enthaelt aber keinen Text"
            )
        prepared.resource_status[resource.id] = status
        if status == KNOWLEDGE_CURRENT:
            prepared.candidates.append(
                _ExcerptCandidate(
                    priority=priority,
                    kind="resource_snapshot",
                    label=f"resource:{_truncate(resource.relative_path, policy.max_label_chars)[0]}",
                    source_id=snapshot.id,
                    text="",
                    truncated=bool(snapshot.truncated),
                    content_hash=snapshot.content_hash,
                    extractor_version=snapshot.extractor_version,
                    snapshot=snapshot,
                    resource_id=resource.id,
                )
            )
        priority += 1


def _load_excerpt_text(
    prepared: _Prepared,
    session: Session,
    store: KnowledgeStore | None,
    candidates: Sequence[_ExcerptCandidate],
    policy: WorkspacePolicy,
) -> None:
    """Fetch text for the candidates that actually survived the excerpt budget.

    The single place that loads any content, for both kinds, and it is bounded by
    ``max_excerpts``. The snapshot metadata scan in :func:`_load_snapshots` excluded
    ``text`` on purpose, and the knowledge candidates were only *identified* so far;
    reading them earlier would have turned one GET into tens of megabytes of I/O for
    text that is then thrown away.
    """
    for candidate in candidates:
        if candidate.text_loaded:
            # Loaded during an earlier budget iteration. A snapshot is immutable, so
            # re-reading it would only cost another query.
            continue
        candidate.text_loaded = True
        if candidate.kind == "resource_snapshot":
            snapshot = session.get(ResourceSnapshot, candidate.source_id)
            if snapshot is None or snapshot.status != "ready" or not snapshot.text:
                prepared.warnings.append(
                    f"resource_snapshot {candidate.source_id}: Text nicht mehr lesbar; der Auszug entfaellt"
                )
                continue
            candidate.text = _truncate(snapshot.text, policy.max_excerpt_chars)[0] or ""
            continue
        if store is None:
            continue
        try:
            read = store.read_excerpt(
                candidate.source_id, policy.max_excerpt_chars, policy.max_knowledge_bytes
            )
        except KnowledgeError as error:
            prepared.warnings.append(
                f"knowledge {candidate.label}: uebersprungen ({type(error).__name__})"
            )
            # ``text_loaded`` was already set, so this runs exactly once per candidate
            # no matter how many reduction passes there are.
            prepared.knowledge_reads_failed += 1
            continue
        if not read.text:
            prepared.warnings.append(
                f"knowledge {candidate.label}: Datei enthaelt keinen Text; der Auszug entfaellt"
            )
            prepared.knowledge_reads_empty += 1
            continue
        candidate.text = read.text
        candidate.truncated = read.truncated
        candidate.content_hash = read.content_hash
