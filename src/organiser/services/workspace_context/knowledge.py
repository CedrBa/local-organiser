"""Select bounded knowledge evidence without scanning original source trees."""

from __future__ import annotations

from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_knowledge_path,
)
from organiser.services.knowledge import (
    KnowledgeError,
    KnowledgeStore,
)
from .constants import CORE_KNOWLEDGE_FILES, _CANONICAL_TOPIC_STEM
from .policy import WorkspacePolicy
from .state import _ExcerptCandidate, _Prepared


def _add_knowledge_candidates(
    prepared: _Prepared, store: KnowledgeStore, policy: WorkspacePolicy
) -> None:
    """Read bounded knowledge excerpts: topic files, core files, then legacy files.

    The physical address always comes from the database (``knowledge_path`` plus
    ``topics/<knowledge_key>.md``), never from a title and never from a document.
    Legacy files are optional, bounded and labelled separately; the DB topic status
    stays canonical either way, and a missing or unreadable file is a warning
    rather than a failure.
    """
    knowledge_path = prepared.context.knowledge_path
    if not knowledge_path:
        prepared.warnings.append(
            "knowledge_excerpts: Context hat keinen gebundenen knowledge_path; es wurde keine Knowledge gelesen"
        )
        return
    try:
        validate_knowledge_path(knowledge_path, allow_reserved_segments=False)
    except KnowledgePathError as error:
        prepared.warnings.append(
            f"knowledge_excerpts: Ungueltiger knowledge_path; es wurde keine Knowledge gelesen ({type(error).__name__})"
        )
        return
    candidates: list[tuple[int, str, str]] = [
        (0, f"topic:{topic.knowledge_key}", f"{knowledge_path}/topics/{topic.knowledge_key}.md")
        for topic in prepared.topics
    ]
    candidates += [
        (1, f"context_core:{name}", f"{knowledge_path}/{name}") for name in CORE_KNOWLEDGE_FILES
    ]
    known_stems = {topic.knowledge_key for topic in prepared.topics}
    try:
        for stem in store.list_topic_files(
            knowledge_path, limit=policy.max_legacy_topic_files_scanned
        ):
            if _CANONICAL_TOPIC_STEM.match(stem) or stem in known_stems:
                continue
            candidates.append((2, f"legacy_topic:{stem}", f"{knowledge_path}/topics/{stem}.md"))
    except KnowledgeError as error:
        prepared.warnings.append(
            f"legacy_topics: Verzeichnis nicht lesbar ({type(error).__name__})"
        )
    prepared.knowledge_candidate_total = len(candidates)
    for rank, label, relative_path in candidates:
        # Identified but deliberately *not* read here. Reading every candidate would
        # mean up to max_topics + 3 + max_legacy_topic_files_scanned reads of up to
        # 256 KiB each -- roughly 20 MiB of disk traffic and 87 SHA-256 passes to
        # render at most four excerpts, on every single authenticated request. The
        # text and its hash are fetched for the winners only, in _load_excerpt_text.
        prepared.candidates.append(
            _ExcerptCandidate(
                priority=rank * 100_000 + len(prepared.candidates) + 1,
                kind="knowledge",
                label=label,
                source_id=relative_path,
                truncated=False,
                content_hash=None,
                extractor_version=None,
            )
        )
