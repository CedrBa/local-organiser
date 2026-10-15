"""Preserve caller work when classifying unique conflicts and using savepoints."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session


def _real_transaction_is_open(session: Session) -> bool:
    """Is a SAVEPOINT opened here guaranteed to nest inside an open transaction?

    SQLAlchemy's ``in_transaction()`` is a *logical* flag: it is already true after
    a read, while SQLite is still in autocommit because a ``SELECT`` opens no
    transaction. The distinction decides whether a SAVEPOINT is safe at all, so it
    has to be asked at the driver level. ``pysqlite`` exposes the real state as
    ``Connection.in_transaction`` (a proxy for ``sqlite3_get_autocommit``).

    Unknown driver or unreachable connection -> ``False``. Failing closed keeps the
    established rollback convention instead of risking an unexpected commit. This
    is the documented residual limit of the SQLite MVP: the guarantee below is
    measured for SQLite/pysqlite, not claimed for every future backend.
    """
    try:
        dbapi_connection = session.connection().connection
    except Exception:  # unbrauchbare/unbekannte Verbindung -> fail closed
        return False
    return bool(getattr(dbapi_connection, "in_transaction", False))


def _flush_caller_state(session: Session) -> None:
    """Move the caller's pending ORM state into a real database transaction.

    Why this runs first
    -------------------
    Two facts about ``pysqlite`` combine badly, and both were measured on this
    project's engine:

    1. A ``SELECT`` opens no SQLite transaction, so a session can hold pending
       caller rows while the driver is still in autocommit
       (``in_transaction()`` is ``True``, ``in_transaction`` is ``False``).
    2. ``Session.begin_nested()`` **pre-flushes** pending state *before* it emits
       ``SAVEPOINT``.

    So a SAVEPOINT is only ever opened here after this flush, which means the
    caller's rows are written into the *enclosing* transaction and not into ours.
    Measured consequence: after ``ROLLBACK TO SAVEPOINT`` the caller's rows are
    still in the session, the transaction is still open, and nothing is visible to
    a second connection.

    Without this step the decision in :func:`_title_uniqueness_boundary` would be
    taken while the driver is in autocommit, fall through to the legacy full
    ``rollback()``, and destroy exactly the pending caller state that was there.

    **Caller-owned.** If this flush fails, the failure belongs to the caller's own
    rows. It is deliberately not caught, not wrapped and not rolled back here:
    cleaning up would destroy the state the caller still has to inspect. The caller
    decides whether to roll back.

    Side effect worth knowing: a domain call may therefore *flush* the caller's
    pending state. It never commits it, and it never invents changes -- flushing is
    what ``session.begin_nested()`` would have done anyway.
    """
    if session.new or session.dirty or session.deleted:
        session.flush()


@contextmanager
def _title_uniqueness_boundary(session: Session) -> Iterator[None]:
    """Contain the *unique* ``title_key`` violation without destroying caller state.

    Scope: this is the rollback boundary for **one** constraint,
    ``topics.uq_topics_context_title_key``, and nothing else. A different
    ``IntegrityError`` is not a duplicate title and must not be reported as one --
    see :func:`_title_key_uniqueness_violated`.

    Why this exists
    ---------------
    A duplicate ``title_key`` can only be *proven* by the database. The ``SELECT``
    in :func:`create_topic` is a pre-check, and it is provably blind to the
    caller's own pending rows, because the session factory runs with
    ``autoflush=False``: the pre-check ``SELECT`` neither flushes nor sees them, and
    the following ``flush()`` writes them together with our row. The resulting
    ``IntegrityError`` on ``topics.uq_topics_context_title_key`` is a real
    occurrence, not a hypothetical -- see
    ``tests/test_workspace_topics_tasks_topics.py``.

    The project-wide convention (``services.domain._commit`` and most services) is
    ``session.rollback()`` on failure. For this one INSERT that is a step too
    coarse: it additionally discards the caller's work and *closes the caller's
    transaction*. Under ``session.info["defer_domain_commit"]`` -- E's proposal
    transaction -- that silently turns "this title is a duplicate" into "the whole
    proposal committed nothing", which is far harder to notice than the conflict
    itself. Measured: with the unconditional rollback the caller's flushed
    ``Resource`` *and* its pending rows were both gone afterwards.

    So a SAVEPOINT is used, but **only when it is provably nested**. This is not
    paranoia, it is measured SQLite behaviour: a SAVEPOINT opened while the
    database is in autocommit *starts* a transaction, and ``RELEASE SAVEPOINT``
    then **commits** it ("if the savepoint was created outside of a transaction,
    then RELEASE behaves like COMMIT", SQLite docs). Measured through this
    project's engine: a ``begin_nested()`` block entered while
    :func:`_real_transaction_is_open` is ``False`` made its row visible to a
    second connection immediately, and that row *survived* a subsequent
    ``session.rollback()``. ``Session.in_transaction()`` does not protect against
    this -- it is already ``True`` after a bare ``SELECT`` while the driver is
    still in autocommit. Unconditionally using a savepoint would therefore commit
    the caller's work from inside a domain function and break the existing
    deferred-rollback guarantee.

    The order matters: :func:`_flush_caller_state` first, then the real
    transaction check. The opposite order was the bug: it decided on a stale
    autocommit reading and took the full-rollback branch with pending caller rows
    present.

    When no real transaction is open there is provably nothing committed to
    protect, so the established ``rollback()`` convention applies unchanged.

    Residual, documented limit: this is measured for SQLite/pysqlite. For an
    unknown driver :func:`_real_transaction_is_open` fails closed to the rollback
    convention, which is safe for committed data but would discard unflushed
    caller state on a backend where a flush does not open a transaction.
    """
    with _constraint_boundary(session):
        yield


@contextmanager
def _constraint_boundary(session: Session) -> Iterator[None]:
    """Isolate one operation's flush while preserving an open caller transaction."""
    _flush_caller_state(session)
    if _real_transaction_is_open(session):
        with session.begin_nested():
            yield
        return
    try:
        yield
    except IntegrityError:
        session.rollback()
        raise


def _title_key_uniqueness_violated(error: IntegrityError) -> bool:
    """Is this ``IntegrityError`` the ``uq_topics_context_title_key`` violation?

    Only *this* one means "duplicate title". Everything else a flush can raise --
    the ``CHECK(knowledge_key = 'topic-' || id)`` guard, a foreign key, a NOT NULL
    column, or the separate ``uq_topics_context_id_knowledge_key`` -- is a defect
    somewhere else, and answering "this title already exists" for it would send the
    caller off renaming something that is not the problem. That is the very
    misclassification the knowledge error classes were split apart for, so it is
    not repeated here: a foreign violation propagates unchanged and the caller's
    own commit/rollback convention decides.

    Measured on SQLite/pysqlite: the driver reports the *columns*, not the
    constraint name -- ``UNIQUE constraint failed: topics.context_id,
    topics.title_key`` -- while a sibling constraint reads
    ``topics.context_id, topics.knowledge_key``. Both spellings plus the named
    constraint are accepted, so a backend that names the constraint is covered too.
    """
    message = str(getattr(error, "orig", error))
    return "uq_topics_context_title_key" in message or "topics.title_key" in message


def _link_key_uniqueness_violated(
    error: IntegrityError, table: str, columns: tuple[str, str]
) -> bool:
    """Recognize only the composite primary key of the requested link table."""
    message = str(getattr(error, "orig", error))
    sqlite_columns = ", ".join(f"{table}.{column}" for column in columns)
    return (
        message == f"UNIQUE constraint failed: {sqlite_columns}"
        or f'constraint "{table}_pkey"' in message
    )
