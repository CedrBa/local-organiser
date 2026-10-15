"""Refuse knowledge addresses outside the exact context binding."""

from __future__ import annotations

from typing import Any
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_knowledge_path,
)
from .redaction import _is_safe_knowledge_path


def _knowledge_address_reason(
    relative_path: str, recorded_path: Any, current_path: Any
) -> str | None:
    """Why a recorded knowledge address may not be read, or ``None`` when it may.

    Three conditions, all necessary and all independent of the address itself:

    1. **Syntax.** The address must be a canonical, relative, store-policy path. This is
       the store's own pure validator, so a traversal, an absolute path, a Windows
       drive or a control character is rejected before any filesystem call. The reader
       confines every path to the root as well, but the point here is to refuse
       *claiming* such a file, not merely to survive reading it.
    2. **The recorded binding.** The address must lie below the ``knowledge_path`` the
       manifest recorded for its own context entity. ``contexts/<id>/`` would be the
       obvious guess, but a context may legitimately be bound to any valid path
       (``kurse/finanzierung``); forcing the canonical layout rejected honest
       manifests of perfectly valid custom bindings.
    3. **The current binding.** The address must also lie below the context's
       *authoritatively current* ``knowledge_path``. This is the condition that closes
       the hole the first one would open: a context that has been rebound since the
       packet was built must not have its old markdown hashed any more. Condition 2
       alone cannot do it, because both values come from the same untrusted record --
       which is also why neither condition turns this into a hash oracle over another
       context's markdown.

    Returns ``None`` when the address may be read.
    """
    if not isinstance(relative_path, str) or not isinstance(recorded_path, str):
        return "no_manifest_context_path"
    if not isinstance(current_path, str) or not current_path:
        return "context_unbound"
    if not _is_safe_knowledge_path(recorded_path) or not _is_safe_knowledge_path(current_path):
        return "invalid_path"
    try:
        validate_knowledge_path(
            relative_path, field="knowledge.relative_path", allow_reserved_segments=True
        )
    except KnowledgePathError:
        return "invalid_path"
    for bound in (recorded_path, current_path):
        if not bound or not relative_path.startswith(f"{bound.rstrip('/')}/"):
            return "foreign_path"
    return None
