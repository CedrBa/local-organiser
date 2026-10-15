"""Deterministic reductions and excerpt selection under serialised packet limits."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence
from sqlalchemy.orm import Session
from organiser.services.knowledge import (
    KnowledgeStore,
)
from .policy import WorkspacePolicy
from .projection import _to_excerpt
from .redaction import _truncate
from .snapshots import _load_excerpt_text
from .state import _ExcerptCandidate, _ExcerptSelection, _Prepared


# ---------------------------------------------------------------------------
# Budget bookkeeping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Budget:
    """How much of each section the serialized packet may still spend.

    The reduction order is fixed: excerpts first (lowest priority first), then
    history, resources, topics, tasks, sources. Every reduction shows up in
    ``omitted_counts`` and ``warnings``. The floor -- all lists empty -- is a small
    packet that always fits, which is what makes the loop terminate; ``strip_optional``
    is the second step below that floor for the case where a bounded field has
    stopped being bounded.
    """

    tasks: int
    topics: int
    resources: int
    sources: int
    events: int
    excerpts: int
    strip_optional: bool = False

    @classmethod
    def initial(cls, policy: WorkspacePolicy) -> "_Budget":
        return cls(
            tasks=policy.max_tasks,
            topics=policy.max_topics,
            resources=policy.max_resources,
            sources=policy.max_sources,
            events=policy.max_events,
            excerpts=policy.max_excerpts,
        )

    def reduced(self) -> "_Budget | None":
        """One deterministic step down, or ``None`` once the floor is reached.

        The *largest* remaining section is halved, with ties broken by the fixed order
        above. Draining one section completely before touching the next would satisfy
        the byte budget just as well but would hand the model a packet with, say, ten
        tasks and no topics, resources, history or evidence at all. Halving the largest
        section keeps the remaining packet balanced and still fully deterministic.

        Excerpts lose exactly one slot instead of half, so the four content excerpts
        disappear one at a time in priority order -- the least relevant text goes
        first, which is what the priority ranking is for.
        """
        candidates = [
            (getattr(self, name), index, name)
            for index, name in enumerate(
                ("excerpts", "events", "resources", "topics", "tasks", "sources")
            )
            if getattr(self, name) > 0
        ]
        if not candidates:
            return None if self.strip_optional else replace(self, strip_optional=True)
        value, _index, name = max(candidates)
        return replace(self, **{name: value - 1 if name == "excerpts" else value // 2})

    def floor(self) -> "_Budget":
        """The smallest packet this module is willing to return.

        Every list empty and the optional context description dropped. Identifying
        fields stay, so the packet remains answerable -- it is just no longer a
        summary. Used when the *work* budget runs out before the byte budget is met:
        stepping one halving at a time would keep re-serialising work we can no
        longer afford.
        """
        return replace(
            self,
            tasks=0,
            topics=0,
            resources=0,
            sources=0,
            events=0,
            excerpts=0,
            strip_optional=True,
        )


def _bounded_warnings(warnings: Sequence[str], policy: WorkspacePolicy) -> tuple[str, ...]:
    """Bound the warning list and say so when warnings had to be dropped.

    The last slot is reserved for that notice. Silently losing a warning -- a stale
    snapshot, a skipped knowledge file, an omitted row -- would make an incomplete
    packet look complete.
    """
    if len(warnings) <= policy.max_warnings:
        kept = warnings
        dropped = 0
    elif policy.max_warnings <= 0:
        kept = []
        dropped = len(warnings)
    else:
        # The last slot is reserved for the "N more omitted" notice itself.
        kept = list(warnings[: policy.max_warnings - 1])
        dropped = len(warnings) - len(kept)
    bounded = [_truncate(warning, policy.max_warning_chars)[0] or "" for warning in kept]
    if dropped and bounded:
        # The notice is itself a warning and has to fit the budget it reports on. A
        # policy that asks for zero warnings gets none -- including this one.
        bounded.append(f"{dropped} weitere Warnungen ausgelassen (Budget)")
    return tuple(bounded)


def _select_excerpts(
    prepared: _Prepared,
    session: Session,
    store: KnowledgeStore | None,
    policy: WorkspacePolicy,
    budget: _Budget,
) -> _ExcerptSelection:
    """Pick excerpts in priority order under the shared and per-kind caps.

    Resources may claim at most ``max_resource_excerpts`` slots whenever a knowledge
    candidate exists, and knowledge then fills the remaining shared budget. The split
    is what guarantees that a context full of extracted documents still shows its
    qualitative markdown; a context with no markdown at all gives the whole budget to
    documents.

    Content is fetched only for the winners. A candidate that turns out to be empty or
    unreadable is dropped with a warning and the next one takes its slot in the
    following budget iteration, so a missing ``overview.md`` cannot silently cost a
    document excerpt.
    """
    resource_candidates = sorted(
        (c for c in prepared.candidates if c.kind == "resource_snapshot"), key=lambda c: c.priority
    )
    knowledge_candidates = sorted(
        (c for c in prepared.candidates if c.kind == "knowledge"), key=lambda c: c.priority
    )
    resource_cap = policy.max_resource_excerpts if knowledge_candidates else budget.excerpts
    # Read ahead by at most ``max_excerpt_read_attempts`` past the budget, so a
    # candidate that fails at read time hands its slot to the next one instead of
    # shrinking the packet. Bounded on purpose: a context full of broken files must
    # stay a bounded amount of work, not the whole candidate list.
    max_attempts = budget.excerpts + policy.max_excerpt_read_attempts
    kept: list[_ExcerptCandidate] = []
    resource_kept = 0
    legacy_kept = 0
    attempts = 0
    for candidate in [*resource_candidates, *knowledge_candidates]:
        if len(kept) >= budget.excerpts or attempts >= max_attempts:
            break
        if candidate.text_loaded and not candidate.text:
            # Already opened in an earlier pass and it yielded nothing. Opening it again
            # cannot help, and charging an attempt for it would starve the next readable
            # candidate for the rest of the request.
            continue
        if candidate.kind == "resource_snapshot":
            if resource_kept >= resource_cap:
                # Already at this kind's share; skipping costs no read attempt.
                continue
        elif (
            candidate.label.startswith("legacy_topic:")
            and legacy_kept >= policy.max_legacy_topic_excerpts
        ):
            continue
        attempts += 1
        _load_excerpt_text(prepared, session, store, [candidate], policy)
        if not candidate.text:
            continue
        kept.append(candidate)
        if candidate.kind == "resource_snapshot":
            resource_kept += 1
        elif candidate.label.startswith("legacy_topic:"):
            legacy_kept += 1
    kept.sort(key=lambda candidate: candidate.priority)
    kept_ids = {id(candidate) for candidate in kept}
    # Release the slot of anything that turned out to be unreadable, so a later budget
    # iteration does not re-choose the broken candidate.
    prepared.candidates = [
        candidate
        for candidate in prepared.candidates
        if not candidate.text_loaded or candidate.kind == "resource_snapshot" or candidate.text
    ]
    # A resource whose text could not be read is treated exactly like one that did
    # not win a slot: it stays in the packet as metadata with evidence_truncated set.
    withheld = {
        candidate.resource_id
        for candidate in resource_candidates
        if candidate.resource_id is not None and id(candidate) not in kept_ids
    }
    # ``identified`` is fixed for the whole request, so the total does not drift from
    # pass to pass the way a count taken from the shrinking candidate list would.
    identified = prepared.resource_candidate_total + prepared.knowledge_candidate_total
    readable_omitted = sum(
        1
        for candidate in prepared.candidates
        if candidate.kind == "knowledge"
        and candidate.text_loaded
        and candidate.text
        and id(candidate) not in kept_ids
    )
    return _ExcerptSelection(
        excerpts=tuple(_to_excerpt(candidate, policy) for candidate in kept),
        withheld_resource_ids=frozenset(withheld),
        omitted=max(0, identified - len(kept)),
        readable_omitted=readable_omitted,
    )
