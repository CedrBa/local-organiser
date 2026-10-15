"""Resource application-boundary guards and owned result transactions."""

from __future__ import annotations

from sqlalchemy.orm import Session
from organiser.domain.resources import (
    ResourceTransactionError,
)


class _ResultTransaction:
    """Own one write transaction and close it cleanly on every failure path."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __enter__(self) -> Session:
        return self._session

    def __exit__(self, exc_type, exc, traceback) -> bool:
        if exc_type is None:
            try:
                self._session.commit()
            except Exception:
                self._session.rollback()
                raise
            return False
        self._session.rollback()
        return False


# -- transaction boundary ----------------------------------------------------


def _require_application_boundary(session: Session) -> None:
    """Refuse to run inside somebody else's transaction.

    Reading a file can take seconds. Doing that while an open transaction holds
    SQLite's write lock would block every other writer of the process, and a
    commit at the end would carry whatever the caller had pending. Both are
    refused here instead of being silently inherited. Foreign work is never
    committed or rolled back by this service.
    """
    if session.info.get("defer_domain_commit"):
        raise ResourceTransactionError(
            "Datei-Zugriff ist in einer offenen Transaktion des Aufrufers nicht erlaubt"
        )
    if session.in_transaction():
        raise ResourceTransactionError(
            "Die Resource-Services sind eigene Application-Transaktionen; bitte die "
            "Transaktion des Aufrufers vorher committen oder zurueckrollen"
        )
    pending = [*session.new, *session.dirty, *session.deleted]
    if pending:
        names = sorted({type(item).__name__ for item in pending})
        raise ResourceTransactionError(
            "Ausstehende ORM-Aenderungen des Aufrufers wuerden mitcommittet: " + ", ".join(names)
        )


def _end_read_transaction(session: Session) -> None:
    """Close a read-only transaction so no lock is held during filesystem work."""
    session.rollback()
    session.expire_all()
