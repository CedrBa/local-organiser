"""Database-only topic creation, rename and status transitions with audit events."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from organiser.db.models import (
    Topic,
    new_id,
)
from organiser.domain.topics import (
    TopicInputError,
    knowledge_key_for,
    normalize_title,
    normalize_title_key,
    validate_topic_status,
)
from organiser.services.domain import (
    UNSET,
    ConflictError,
    EventContext,
    _changes,
    _commit,
    record_event,
)
from .bindings import _context_or_error, _topic_input, _topic_or_error
from .constraints import (
    _flush_caller_state,
    _title_key_uniqueness_violated,
    _title_uniqueness_boundary,
)
from .links import (
    DEFAULT_TOPIC_STATUS,
    TOPIC_CREATED_EVENT,
    TOPIC_STATUS_CHANGED_EVENT,
    TOPIC_UPDATED_EVENT,
)


# -- lifecycle ----------------------------------------------------------------


def create_topic(
    session: Session,
    *,
    context_id: str,
    title: str,
    status: str = DEFAULT_TOPIC_STATUS,
    context: EventContext | None = None,
) -> Topic:
    """Create a topic in an existing context. Database only, no file is written.

    The identifier is generated first and the knowledge key is derived from it
    afterwards, so ``knowledge_key`` is a pure function of the primary key and
    matches the ``CHECK(knowledge_key = 'topic-' || id)`` of the migration.

    A second topic with the same ``title_key`` in the same context is a
    :class:`ConflictError` rather than a silent rename: the user asked for two
    distinct topics, and merging them is their decision. The same title in a
    *different* context is fine and expected.

    The duplicate ``SELECT`` is a **pre-check only**, and it deliberately runs
    *before* :func:`_flush_caller_state`. Refusing an obviously duplicate title
    must not touch the caller's pending state at all, and the price is that the
    pre-check stays blind to those rows -- which is exactly why the boundary below
    exists. :func:`update_topic` flushes first and therefore *does* see them; the
    two orders are both deliberate, so neither is presented here as the general
    rule.
    """
    existing_context = _context_or_error(session, context_id)
    try:
        display_title = normalize_title(title)
        title_key = normalize_title_key(title)
        status = validate_topic_status(status)
    except TopicInputError as error:
        raise _topic_input(error) from error

    duplicate = session.scalar(
        select(Topic.id)
        .where(Topic.context_id == existing_context.id, Topic.title_key == title_key)
        .limit(1)
    )
    if duplicate is not None:
        raise ConflictError("Ein Topic mit diesem Titel existiert im Context bereits")

    identifier = new_id()
    topic = Topic(
        id=identifier,
        context_id=existing_context.id,
        title=display_title,
        title_key=title_key,
        status=status,
        knowledge_key=knowledge_key_for(identifier),
    )
    # Caller-owned, deliberately outside the ``try`` below: getting the caller's
    # pending rows into a real transaction is a precondition for the boundary, and
    # a failure here belongs to *those* rows. Labelling it as a duplicate title
    # would be wrong, and rolling it back would destroy state the caller still has
    # to inspect.
    _flush_caller_state(session)
    try:
        with _title_uniqueness_boundary(session):
            session.add(topic)
            session.flush()
    except IntegrityError as error:
        # Only the title-key uniqueness violation is a duplicate title. Anything
        # else the flush raises belongs to somebody else's rows and propagates.
        if not _title_key_uniqueness_violated(error):
            raise
        raise ConflictError("Ein Topic mit diesem Titel existiert im Context bereits") from error
    record_event(
        session,
        event_type=TOPIC_CREATED_EVENT,
        entity_type="topic",
        entity_id=topic.id,
        payload={
            "initial": {
                "context_id": topic.context_id,
                "title": topic.title,
                "title_key": topic.title_key,
                "status": topic.status,
                "knowledge_key": topic.knowledge_key,
            }
        },
        context=context,
    )
    _commit(session)
    return topic


def update_topic(
    session: Session,
    *,
    topic_id: str,
    title: str | object = UNSET,
    context: EventContext | None = None,
) -> Topic:
    """Rename a topic. The identifier and the knowledge key never change.

    ``knowledge_key`` is not a parameter and never appears in the change set: a
    rename is a label change, so it moves no file and breaks no link.

    No-op semantics are exact
    -------------------------
    ``title_key`` is recomputed **only for an actual rename**, i.e. only when the
    normalised display title really differs from the stored one. Two cases are
    therefore true no-ops that touch neither the row, nor ``updated_at``, nor the
    audit log:

    * ``title`` is not given at all (``{}`` PATCH), and
    * ``title`` normalises to the stored *display* title (``"  Bilanzierung  "``).

    Case belongs to the display title -- only ``title_key`` folds case -- so
    ``"BILANZIERUNG"`` on ``"Bilanzierung"`` is a real rename that happens to keep
    the same key.

    This matters for rows whose stored ``title_key`` is not the canonical folding
    of their stored title -- historically or manually written ones. Recomputing
    the key on an empty PATCH would be a silent repair of somebody else's data:
    it would change a row the caller never mentioned, bump its ``updated_at`` (and
    with it the manifest version D caches), emit a ``TOPIC_UPDATED`` event, and it
    could even turn into a ``ConflictError`` against a second non-canonical row
    that a human still has to reconcile. Repair stays an explicit act: a real
    rename normalises the key, a no-op does not.
    """
    topic = _topic_or_error(session, topic_id)
    if title is UNSET:
        return topic
    try:
        new_title = normalize_title(title)
    except TopicInputError as error:
        raise _topic_input(error) from error
    if new_title == topic.title:
        return topic
    # Ab hier ist es ein echtes Rename: der Key wird kanonisch neu gefaltet.
    #
    # Die Umwandlung ist defensiv und derzeit **nicht erreichbar**: gemessen ueber
    # alle Unicode-Codepoints gibt es keinen Titel, den ``normalize_title``
    # annimmt und dessen NFKC/casefold-Faltung leer wird -- der leere-Key-Guard in
    # ``normalize_title_key`` kann also nach erfolgreichem ``normalize_title``
    # nicht feuern. Sie bleibt trotzdem, weil ``normalize_title_key``
    # ``normalize_title`` intern erneut aufruft und eine zukuenftige Domain-Regel
    # dazwischenrutschen kann. Ohne sie escape eine rohe ``TopicInputError`` (ein
    # ``ValueError``) aus diesem Service und damit als 500 statt der zugesagten 422
    # -- die Umwandlung ist die Zusage, nicht der konkrete Ausloeser.
    try:
        new_title_key = normalize_title_key(new_title)
    except TopicInputError as error:
        raise _topic_input(error) from error

    changes = _changes(title=(topic.title, new_title), title_key=(topic.title_key, new_title_key))
    if not changes:
        return topic
    # Caller-owned, and deliberately *before* our own mutation: flushing first moves
    # the caller's staged rows into the enclosing transaction, so neither they nor we
    # are disturbed later. Flushing after the ``setattr`` below would trip over our
    # own dirty row and misreport the caller's insert as our failure.
    #
    # Here the order is the mirror image of ``create_topic``: the pre-check below
    # runs *after* the flush and therefore does see the caller's staged topics, so a
    # staged collision is refused before anything is mutated. A committed rival that
    # arrives between this pre-check and the flush is still only decided by the
    # database -- that is the boundary's job, not this SELECT's.
    _flush_caller_state(session)
    if new_title_key != topic.title_key:
        clash = session.scalar(
            select(Topic.id)
            .where(
                Topic.context_id == topic.context_id,
                Topic.title_key == new_title_key,
                Topic.id != topic.id,
            )
            .limit(1)
        )
        if clash is not None:
            raise ConflictError("Ein Topic mit diesem Titel existiert im Context bereits")
    # Mutation, audit and flush share one operation boundary. The clash SELECT above
    # is a pre-check, so the real ``UNIQUE`` violation -- a rival that committed
    # between pre-check and flush -- is only decided by this flush. Leaving it to
    # ``_commit`` would route it through ``_commit``'s ``session.rollback()`` and
    # destroy the caller's whole deferred proposal transaction instead of undoing
    # just this rename.
    try:
        with _title_uniqueness_boundary(session):
            for field, change in changes.items():
                setattr(topic, field, change["new"])
            record_event(
                session,
                event_type=TOPIC_UPDATED_EVENT,
                entity_type="topic",
                entity_id=topic.id,
                payload={"changes": changes},
                context=context,
            )
            session.flush()
    except IntegrityError as error:
        # Same rule as ``create_topic``: only the title-key uniqueness violation is
        # a duplicate title; any other one propagates instead of being renamed into
        # one.
        if not _title_key_uniqueness_violated(error):
            raise
        raise ConflictError("Ein Topic mit diesem Titel existiert im Context bereits") from error
    # The outer commit stays outside the boundary, so no boundary failure can make it
    # roll the caller's transaction back.
    _commit(session)
    return topic


def set_topic_status(
    session: Session, *, topic_id: str, status: str, context: EventContext | None = None
) -> Topic:
    """Set one of the four coarse topic statuses.

    There is deliberately no transition table. "Not started" and "done" are
    planning statements the user revises freely, and a guard rail here would only
    block a legitimate correction. The four values are a coarse planning marker;
    the qualitative learning state stays in markdown.

    Setting the same value again is a no-op without an event, so a repeated call
    cannot flood the audit log. Nothing else is touched: no task is completed,
    and the context status is independent.
    """
    topic = _topic_or_error(session, topic_id)
    try:
        status = validate_topic_status(status)
    except TopicInputError as error:
        raise _topic_input(error) from error
    if topic.status == status:
        return topic
    old_status = topic.status
    topic.status = status
    record_event(
        session,
        event_type=TOPIC_STATUS_CHANGED_EVENT,
        entity_type="topic",
        entity_id=topic.id,
        payload={"changes": _changes(status=(old_status, status))},
        context=context,
    )
    _commit(session)
    return topic
