"""Fixed packet limits and scope/budget refusals; limits can only be narrowed."""

from __future__ import annotations

from dataclasses import dataclass, replace
from organiser.services.domain import DomainValidationError
from organiser.services.knowledge import (
    MAX_EXCERPT_BYTES,
)


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkspacePolicy:
    """Fixed bounds of the workspace read model.

    These are limits, not defaults. The HTTP layer never accepts them from a
    request, and :meth:`clamped` caps any policy at these values, so no caller and
    no client can widen the packet. A *smaller* policy is allowed on purpose: it is
    the intended way to ask for less.
    """

    max_tasks: int = 20
    max_topics: int = 20
    max_resources: int = 20
    max_sources: int = 10
    max_events: int = 20
    max_excerpts: int = 4
    # How many *additional* candidates may be opened when a chosen one turns out to be
    # unreadable, so a single oversized markdown file cannot silently cost a document
    # excerpt. Bounded, so a context full of broken files stays a bounded amount of
    # work rather than the full candidate list.
    max_excerpt_read_attempts: int = 4
    # Resource text and knowledge text share the excerpt budget. When both kinds have
    # candidates the budget is split evenly, so a document-heavy context can never
    # crowd the qualitative knowledge out of the packet entirely. With no knowledge
    # candidate at all, resources may use the whole budget.
    max_resource_excerpts: int = 2
    max_excerpt_chars: int = 4_000
    max_packet_chars: int = 32_000

    max_title_chars: int = 200
    max_description_chars: int = 600
    max_label_chars: int = 200
    max_warning_chars: int = 300
    max_warnings: int = 20
    max_event_type_chars: int = 120
    max_event_entity_chars: int = 120
    max_event_payload_keys: int = 12
    max_event_payload_items: int = 10
    max_event_payload_depth: int = 3
    max_event_value_chars: int = 200
    max_event_key_chars: int = 100
    # Hard SQL bound on the event payload **materialised per selected event**.
    #
    # ``events.payload`` is the only column in the schema without a size constraint,
    # and a legacy ``CONTEXT_SOURCE_ADDED`` row carries the absolute host path it
    # registered plus whatever root texts the writer felt like storing. Loading such
    # a column turns one GET into a JSON deserialisation of megabytes -- per event,
    # and the projection was recomputed on every budget pass on top of that. The size
    # is therefore measured in SQL and the blob is not handed to Python at all above
    # this limit; the event keeps its type, time and *measured length* and says
    # visibly that its payload was not loaded. 32 KiB is far above any payload the
    # bounded projection can actually show (at most ~12 keys x 200 characters), and it
    # keeps small, working events -- the ones with real provenance -- fully projected.
    max_event_payload_bytes: int = 32 * 1024
    max_ids_per_item: int = 5
    max_knowledge_bytes: int = MAX_EXCERPT_BYTES
    # Bounded, separately labelled legacy topic files. Never part of the canonical
    # topic selection and never required for the packet to be complete.
    max_legacy_topic_excerpts: int = 3
    max_legacy_topic_files_scanned: int = 64
    # Hard SQL bound for the snapshot metadata scan, **per resource**. Snapshot text
    # is never part of that scan; it is fetched separately, only for resources that
    # may contribute an excerpt. A per-resource bound rather than a global one: a
    # resource with hundreds of snapshots must not be able to push another
    # resource's current evidence out of the window.
    max_snapshots_per_resource: int = 10
    # Bounded number of explicitly focusable IDs per kind.
    max_focus_ids: int = 20
    # *Total* work bounds for the reduction loop, not just the final packet size.
    #
    # The ladder needs at most one halving per section plus one slot per excerpt,
    # which is ~29 steps; both numbers sit comfortably above that, so they never cut a
    # legitimate packet short. They exist so that a future change to the projection
    # cannot silently turn one authenticated GET into an unbounded amount of
    # serialisation: past either limit the loop stops stepping and jumps straight to
    # the floor budget, then fails closed.
    max_assemble_passes: int = 32
    max_serialized_chars_total: int = 1_000_000
    # The task-packet excerpt is a summary, not a second full packet. These cap the
    # parts that the full-packet limits above do not already govern.
    summary_max_topics: int = 5
    summary_max_evidence: int = 4
    summary_max_warnings: int = 5

    def clamped(self) -> "WorkspacePolicy":
        """Never exceed the architecture's hard limits, whatever a caller passed.

        Clamped from *above* and floored at zero. Without the floor a negative value
        would invert a bound instead of shrinking it: ``candidates[:-1]`` would keep
        every candidate but the last, and a policy claiming ``max_excerpts = -1``
        would emit nine excerpts against a hard cap of four.
        """
        hard = WorkspacePolicy()
        return replace(
            self,
            **{
                name: max(0, min(getattr(self, name), getattr(hard, name)))
                for name in hard.__dataclass_fields__
            },
        )


DEFAULT_WORKSPACE_POLICY = WorkspacePolicy()


class WorkspaceSelectionError(DomainValidationError):
    """An explicitly requested topic/resource ID does not belong to this context.

    Raised instead of silently dropping or relocating the ID. Resolving a foreign ID
    by title similarity would be exactly the fuzzy cross-context roaming the
    architecture forbids, and silently dropping it would hide a caller bug.
    """


class WorkspaceBudgetError(DomainValidationError):
    """The packet could not be brought below ``max_packet_chars``.

    Fail-closed on purpose. The reduction ladder provably reaches a floor far below
    32.000 characters, so reaching this error means the bounded fields of the packet
    are no longer bounded -- a defect in this module, not a data condition. Returning
    an oversized packet anyway would quietly break the one guarantee a model-facing
    read model has to make.
    """
