"""Shared fixtures and builders for test_workspace_context contracts; no executable tests."""

from __future__ import annotations

from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import finance as finance, store as store, workspace_env as workspace_env


def build(env, policy=None, **kwargs):
    """Build the packet of the acceptance fixture's context."""
    return workspace_context(env["session"], env["context"].id, env["store"], policy, **kwargs)


def labels(packet) -> list[str]:
    return [excerpt.label for excerpt in packet.knowledge_excerpts]


def hashlib_hex(index: int) -> str:
    return f"{index:064x}"[-64:]
