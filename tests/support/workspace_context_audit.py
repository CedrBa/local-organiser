"""Shared fixtures and builders for test_workspace_context_audit contracts; no executable tests."""

from __future__ import annotations

from organiser.services.workspace_context import (
    workspace_context,
)
from support.workspace import workspace_env as workspace_env
from support.workspace import finance as finance, store as store


def build(env, policy=None, **kwargs):
    return workspace_context(env["session"], env["context"].id, env["store"], policy, **kwargs)
