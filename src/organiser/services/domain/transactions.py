"""Commit or deferred-flush boundary owned by the calling application."""

from __future__ import annotations

from sqlalchemy.orm import Session


def _commit(session: Session) -> None:
    """Flush under application ownership; otherwise commit, rolling back on failure."""
    try:
        if session.info.get("defer_domain_commit"):
            session.flush()
            return
        session.commit()
    except Exception:
        session.rollback()
        raise
