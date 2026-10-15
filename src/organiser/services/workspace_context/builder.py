"""Assemble one read-only workspace packet from bounded prepared data."""

from __future__ import annotations

from dataclasses import replace
from typing import Sequence
from sqlalchemy.orm import Session
from organiser.db.models import (
    Resource,
    Topic,
)
from organiser.domain.context import (
    ContextSummary,
    ContextWorkspacePacket,
    WorkspaceEvidenceRef,
    WorkspaceExcerptSummary,
    WorkspaceFreshness,
    WorkspaceTopicRef,
)
from organiser.domain.workspace_contracts import (
    WorkspaceSelection,
)
from organiser.services.knowledge import (
    KnowledgeStore,
)
from .budget import _Budget, _bounded_warnings, _select_excerpts
from .knowledge import _add_knowledge_candidates
from .manifest import _build_manifest
from .policy import DEFAULT_WORKSPACE_POLICY, WorkspaceBudgetError, WorkspacePolicy
from .projection import (
    _event_summary,
    _packet_evidence,
    _resource_summary,
    _source_summary,
    _task_summary,
    _topic_summary,
)
from .redaction import _safe_knowledge_path, _truncate
from .selection import _load_context, _prepare, _validate_focus
from .state import _Prepared


# ---------------------------------------------------------------------------
# Public builder
# ---------------------------------------------------------------------------


def workspace_context(
    session: Session,
    context_id: str,
    store: KnowledgeStore | None,
    policy: WorkspacePolicy | None = None,
    *,
    topic_ids: Sequence[str] = (),
    resource_ids: Sequence[str] = (),
) -> ContextWorkspacePacket:
    """Build the bounded, read-only workspace packet of exactly one context.

    ``store`` may be ``None``: the database half of the packet is then still
    complete and every knowledge excerpt is replaced by a visible warning. A context
    without an initialised knowledge store stays usable, which the architecture
    requires explicitly.
    """
    policy = (policy or DEFAULT_WORKSPACE_POLICY).clamped()
    context = _load_context(session, context_id)
    wanted_topics = _validate_focus(session, context, Topic, "topic_ids", topic_ids, policy)
    wanted_resources = _validate_focus(
        session, context, Resource, "resource_ids", resource_ids, policy
    )

    prepared = _prepare(session, context, policy, wanted_topics, wanted_resources)
    if store is None:
        prepared.warnings.append(
            "knowledge_excerpts: kein Knowledge-Store uebergeben; es wurde keine Knowledge-Datei gelesen"
        )
    else:
        _add_knowledge_candidates(prepared, store, policy)
    return _assemble(session, prepared, store, policy, wanted_topics, wanted_resources)


def workspace_excerpt_summary(
    session: Session,
    context_id: str,
    store: KnowledgeStore | None = None,
    policy: WorkspacePolicy | None = None,
    *,
    topic_ids: Sequence[str] = (),
    resource_ids: Sequence[str] = (),
) -> WorkspaceExcerptSummary:
    """Small, metadata-only workspace excerpt for an existing ``ContextPacket``.

    Derived from the same selection and the same budget as the full packet and then
    reduced further, so a task packet can never outgrow its own limit. It carries no
    document or knowledge text: the interpreter and planner prompts are built from
    that packet, and untrusted course documents do not belong in them.
    """
    policy = (policy or DEFAULT_WORKSPACE_POLICY).clamped()
    # No content excerpts at all: this DTO has no text field, so reading a knowledge
    # file here would be pure I/O for something the caller cannot see. Evidence
    # references still come from the resource snapshot pointers.
    summary_policy = replace(policy, max_excerpts=0, max_resource_excerpts=0)
    packet = workspace_context(
        session, context_id, store, summary_policy, topic_ids=topic_ids, resource_ids=resource_ids
    )
    return WorkspaceExcerptSummary(
        context_id=packet.context.id,
        title=packet.context.title,
        type=packet.context.type,
        status=packet.context.status,
        knowledge_bound=packet.context.knowledge_bound,
        open_task_count=packet.context.open_task_count,
        topic_count=packet.context.topic_count,
        resource_count=packet.context.resource_count,
        topics=tuple(
            WorkspaceTopicRef(id=topic.id, title=topic.title, status=topic.status)
            for topic in packet.topics[: policy.summary_max_topics]
        ),
        evidence=tuple(
            WorkspaceEvidenceRef(
                kind=item.kind,
                source_id=item.source_id,
                content_hash=item.content_hash,
                extractor_version=item.extractor_version,
                locator=item.locator,
                truncated=item.truncated,
            )
            for item in packet.evidence[: policy.summary_max_evidence]
        ),
        warnings=tuple(
            _truncate(warning, policy.max_warning_chars)[0] or ""
            for warning in packet.warnings[: policy.summary_max_warnings]
        ),
    )


# ---------------------------------------------------------------------------
# Assembly, budget enforcement and manifest
# ---------------------------------------------------------------------------


def _assemble(
    session: Session,
    prepared: _Prepared,
    store: KnowledgeStore | None,
    policy: WorkspacePolicy,
    wanted_topics: tuple[str, ...],
    wanted_resources: tuple[str, ...],
) -> ContextWorkspacePacket:
    """Assemble the packet, shrinking deterministically until the JSON budget holds.

    The loop is the *only* place that decides how much fits, and it measures the
    real serialized packet rather than estimating it. Because the floor -- every
    list empty, only the context summary, selection, manifest and a handful of
    warnings -- is far below 32.000 characters, the loop always terminates.

    Two budgets, not one. ``max_packet_chars`` bounds the *result*;
    ``max_assemble_passes`` and ``max_serialized_chars_total`` bound the *work*, which
    is what a request actually costs. Both sit above what the reduction ladder needs,
    so a legitimate packet is never cut short -- but if either is reached the loop
    stops stepping one halving at a time and goes straight to the floor, and if even
    that does not fit it fails closed. An unbounded reduce loop over an unbounded
    projection is how one GET turned into half a minute of work.
    """
    budget = _Budget.initial(policy)
    passes = 0
    spent = 0
    while True:
        passes += 1
        packet = _assemble_once(
            session,
            prepared,
            store,
            policy,
            budget,
            wanted_topics=wanted_topics,
            wanted_resources=wanted_resources,
        )
        serialized = len(packet.model_dump_json())
        spent += serialized
        if serialized <= policy.max_packet_chars:
            return packet
        reduced = budget.reduced()
        floor = budget.floor()
        if (
            reduced is not None
            and reduced != floor
            and (passes >= policy.max_assemble_passes or spent >= policy.max_serialized_chars_total)
        ):
            reduced = floor
        if reduced is None:
            raise WorkspaceBudgetError(
                f"Das Workspace-Packet fuer Context {prepared.context.id} laesst sich auch im "
                f"reduzierten Zustand nicht auf {policy.max_packet_chars} Zeichen begrenzen "
                f"({serialized} Zeichen). Es wird kein ungegrenztes Packet zurueckgegeben."
            )
        budget = reduced


def _assemble_once(
    session: Session,
    prepared: _Prepared,
    store: KnowledgeStore | None,
    policy: WorkspacePolicy,
    budget: _Budget,
    wanted_topics: tuple[str, ...],
    wanted_resources: tuple[str, ...],
) -> ContextWorkspacePacket:
    """Project one deterministic packet under the current budget reduction state."""
    selection = _select_excerpts(prepared, session, store, policy, budget)
    excerpts = list(selection.excerpts)
    withheld_resource_ids = selection.withheld_resource_ids
    excerpt_omitted = selection.omitted
    context_description = prepared.context.description
    # Below the list floor only the optional free text of the context summary is
    # dropped. Everything identifying stays, so the packet remains usable.
    description_limit = 0 if budget.strip_optional else policy.max_description_chars
    tasks = [_task_summary(task, prepared, policy) for task in prepared.tasks[: budget.tasks]]
    topics = [_topic_summary(topic, prepared, policy) for topic in prepared.topics[: budget.topics]]
    resources = [
        _resource_summary(resource, prepared, policy, resource.id in withheld_resource_ids)
        for resource in prepared.resources[: budget.resources]
    ]
    sources = [_source_summary(source, policy) for source in prepared.sources[: budget.sources]]
    events = [_event_summary(event, policy) for event in prepared.events[: budget.events]]

    omitted: dict[str, int] = {}
    warnings = list(prepared.warnings)
    if budget.strip_optional:
        warnings.append("context.description: ausgelassen (Budget)")

    def note(key: str, total: int, kept: int) -> None:
        if total - kept > 0:
            omitted[key] = total - kept
            warnings.append(f"{key}: {total - kept} Eintraege ausgelassen (Budget)")

    note("tasks", prepared.counts["open_tasks"], len(tasks))
    note("topics", prepared.counts["topics"], len(topics))
    note("resources", prepared.counts["resources"], len(resources))
    note("sources", prepared.counts["sources"], len(sources))
    note("events", prepared.counts["events"], len(events))
    if excerpt_omitted:
        omitted["excerpts"] = excerpt_omitted
        warnings.append(f"excerpts: {excerpt_omitted} Inhaltsauszuege ausgelassen (Budget)")
    # Three disjoint reasons, all subsets of ``excerpts``, none of them the whole
    # candidate total. The distinction matters because they call for different actions:
    # a budget omission is harmless, an unreadable file is a fact about the store, and a
    # file nobody opened proves nothing at all. A single cumulative read counter could
    # only report "reads happened", so a readable ``overview.md`` that lost its slot was
    # announced as "no knowledge file is readable" -- a data-loss claim about a file
    # that was sitting right there.
    if selection.readable_omitted:
        omitted["knowledge_excerpts"] = selection.readable_omitted
        warnings.append(
            f"knowledge_excerpts: {selection.readable_omitted} Knowledge-Dateien wurden gelesen, "
            "sind lesbar und trotzdem aus Budgetgruenden nicht im Packet"
        )
    if prepared.knowledge_reads_empty:
        omitted["knowledge_empty"] = prepared.knowledge_reads_empty
        warnings.append(
            f"knowledge_empty: {prepared.knowledge_reads_empty} Knowledge-Dateien vorhanden, aber ohne Text"
        )
    if prepared.knowledge_reads_failed:
        omitted["knowledge_unreadable"] = prepared.knowledge_reads_failed
        warnings.append(
            f"knowledge_unreadable: {prepared.knowledge_reads_failed} Knowledge-Dateien vorhanden, aber nicht lesbar"
        )
    payloads_not_loaded = prepared.counts.get("event_payloads", 0)
    if payloads_not_loaded:
        omitted["event_payloads"] = payloads_not_loaded
        warnings.append(
            f"event_payloads: {payloads_not_loaded} Event-Payloads ueber der Lesegrenze von "
            f"{policy.max_event_payload_bytes} Zeichen wurden nicht geladen; die Ereignisse selbst "
            "bleiben mit Typ, Zeit und gemessener Laenge sichtbar"
        )
    if withheld_resource_ids:
        omitted["resource_excerpt_texts"] = len(withheld_resource_ids)
        warnings.append(
            f"resource_excerpt_texts: {len(withheld_resource_ids)} aktuelle Ressourcen ohne Text im Packet; "
            "sie bleiben ueber resource_id und snapshot_id nachlesbar"
        )
    for key, total, links in (
        (
            "task_resource_links",
            prepared.counts.get("task_resource_links", 0),
            prepared.task_resource_links,
        ),
        (
            "topic_resource_links",
            prepared.counts.get("topic_resource_links", 0),
            prepared.topic_resource_links,
        ),
    ):
        kept = sum(len(values) for values in links.values())
        if total > kept:
            omitted[key] = total - kept
            warnings.append(f"{key}: {total - kept} Verknuepfungen ausgelassen (Budget)")

    if budget.strip_optional:
        omitted["context_description"] = len(context_description or "")
    context = prepared.context
    manifest = _build_manifest(
        context, prepared, policy, tasks, topics, resources, sources, events, excerpts
    )
    return ContextWorkspacePacket(
        context=ContextSummary(
            id=context.id,
            title=_truncate(context.title, policy.max_title_chars)[0] or "",
            type=_truncate(context.type, policy.max_title_chars)[0] or "",
            status=_truncate(context.status, policy.max_title_chars)[0] or "",
            description=_truncate(context.description, description_limit)[0],
            parent_context_id=context.parent_context_id,
            knowledge_path=_safe_knowledge_path(context.knowledge_path),
            knowledge_bound=bool(context.knowledge_path),
            task_count=prepared.counts["tasks"],
            open_task_count=prepared.counts["open_tasks"],
            topic_count=prepared.counts["topics"],
            resource_count=prepared.counts["resources"],
            source_count=prepared.counts["sources"],
        ),
        current_tasks=tuple(tasks),
        topics=tuple(topics),
        resources=tuple(resources),
        source_descriptions=tuple(sources),
        knowledge_excerpts=tuple(excerpts),
        evidence=_packet_evidence(resources, excerpts, policy),
        recent_history=tuple(events),
        selection=WorkspaceSelection(
            context_id=context.id,
            topic_ids=wanted_topics,
            resource_ids=wanted_resources,
            omitted_counts=dict(sorted(omitted.items())),
        ),
        warnings=_bounded_warnings(warnings, policy),
        manifest=manifest,
        freshness=WorkspaceFreshness(event_max_id=prepared.event_max_id),
    )
