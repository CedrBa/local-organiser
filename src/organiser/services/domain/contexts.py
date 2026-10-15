"""Context lifecycle, hierarchy and immutable knowledge-path bindings."""

from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session
from organiser.db.models import (
    Context,
)
from organiser.domain.knowledge_path import (
    KnowledgePathError,
    validate_context_path,
)
from .errors import ConflictError, DomainValidationError, NotFoundError
from .events import EventContext, _changes, record_event
from .queries import _context_or_error
from .transactions import _commit
from .validation import CONTEXT_STATUSES, CONTEXT_STATUS_TRANSITIONS, UNSET, _required_title


def _required_type(context_type: str) -> str:
    if not isinstance(context_type, str):
        raise DomainValidationError("type darf nicht leer sein")
    normalized = context_type.strip()
    if not normalized:
        raise DomainValidationError("type darf nicht leer sein")
    return normalized


def _normalized_knowledge_path(knowledge_path: str | None) -> str | None:
    """Prueft einen Context-Wissenspfad als reinen Verweis auf den Store.

    Der Pfad wird hier weder aufgeloest noch angelegt: die Funktion liest keine
    Settings und beruehrt das Dateisystem nicht. Sie normalisiert auch nichts
    still, sondern weist jede mehrdeutige Schreibweise zurueck, damit derselbe
    Speicherort nicht unter zwei Namen entstehen kann.

    Die Regeln sind dieselben wie im KnowledgeStore (siehe
    ``organiser.domain.knowledge_path``), damit ein hier akzeptierter Wert dort
    garantiert gueltig ist und umgekehrt. ``None`` bedeutet "noch nicht gebunden".
    """
    if knowledge_path is None:
        return None
    try:
        return validate_context_path(knowledge_path)
    except KnowledgePathError as error:
        raise DomainValidationError(str(error)) from error


def _validate_context_status(status: str) -> str:
    if status not in CONTEXT_STATUSES:
        raise DomainValidationError("status ist ungueltig")
    return status


def _validate_context_parent(
    session: Session, *, context_id: str | None, parent_context_id: str | None
) -> str | None:
    """Reject missing parents, self-parenting and cycles before reparenting."""
    if parent_context_id is None:
        return None
    if parent_context_id == context_id:
        raise DomainValidationError("Ein Context kann nicht sein eigener Parent sein")
    cursor = _context_or_error(session, parent_context_id)
    while cursor.parent_context_id is not None:
        if cursor.parent_context_id == context_id:
            raise DomainValidationError("Context-Hierarchie darf keinen Zyklus enthalten")
        cursor = _context_or_error(session, cursor.parent_context_id)
    return parent_context_id


def _committed_knowledge_path(session: Session, context_id: str) -> str | None:
    """Liest die Bindung direkt aus der Datenbank, nicht aus der Identitaetskarte.

    Ein Context-Objekt kann lange im Speicher liegen und dann einen veralteten
    ``NULL`` zeigen. Genau daraus wuerde ein PATCH eine bereits vorhandene Bindung
    ueberschreiben -- und damit geschriebenes Wissen von seinem Verweis trennen.
    Die Spaltenabfrage umgeht die Identitaetskarte und liest genau den Stand, den
    auch eine andere Session sehen wuerde.

    Innerhalb der laufenden Transaktion des Aufrufers sieht sie dessen eigene,
    geflushte Aenderungen mit; das ist gewuenscht.
    """
    row = session.execute(
        select(Context.id, Context.knowledge_path).where(Context.id == context_id)
    ).one_or_none()
    if row is None:
        raise NotFoundError("Context nicht gefunden")
    return row.knowledge_path


def _assert_knowledge_path_available(
    session: Session, knowledge_path: str | None, *, context_id: str | None = None
) -> str | None:
    """Sichert zu, dass ein gebundener Wissensspeicher genau einem Context gehoert.

    ``NULL`` bedeutet "noch nicht gebunden" und ist deshalb nie ein Konflikt. Die
    Datenbank erzwingt dieselbe Regel ueber einen partiellen Unique-Index
    (Migration ``0009``); diese Pruefung liefert die verstaendliche Meldung, bevor
    der IntegrityError entsteht.
    """
    if knowledge_path is None:
        return None
    statement = select(Context.id).where(Context.knowledge_path == knowledge_path).limit(1)
    if context_id is not None:
        statement = statement.where(Context.id != context_id)
    owner = session.scalar(statement)
    if owner is not None:
        raise ConflictError(f"knowledge_path ist bereits an Context {owner} gebunden")
    return knowledge_path


def create_context(
    session: Session,
    *,
    title: str,
    type: str,
    description: str | None = None,
    parent_context_id: str | None = None,
    knowledge_path: str | None = None,
    context: EventContext | None = None,
) -> Context:
    """Legt einen Context an; Quellen koennen bewusst spaeter hinzukommen.

    Ein hier explizit gesetzter ``knowledge_path`` ist dauerhaft: er bindet den
    Wissensspeicher und wird weder durch ``update_context`` noch durch Umbenennung
    oder Reparenting veraendert.
    """
    parent_context_id = _validate_context_parent(
        session, context_id=None, parent_context_id=parent_context_id
    )
    knowledge_path = _assert_knowledge_path_available(
        session, _normalized_knowledge_path(knowledge_path)
    )
    created = Context(
        title=_required_title(title),
        description=description,
        type=_required_type(type),
        status="active",
        parent_context_id=parent_context_id,
        knowledge_path=knowledge_path,
    )
    session.add(created)
    session.flush()
    record_event(
        session,
        event_type="CONTEXT_CREATED",
        entity_type="context",
        entity_id=created.id,
        payload={
            "initial": {
                "title": created.title,
                "description": created.description,
                "type": created.type,
                "status": created.status,
                "parent_context_id": created.parent_context_id,
                "knowledge_path": created.knowledge_path,
            }
        },
        context=context,
    )
    _commit(session)
    return created


def update_context(
    session: Session,
    *,
    context_id: str,
    title: str | object = UNSET,
    description: str | None | object = UNSET,
    type: str | object = UNSET,
    parent_context_id: str | None | object = UNSET,
    knowledge_path: str | None | object = UNSET,
    context: EventContext | None = None,
) -> Context:
    """Update supplied context fields, preserving hierarchy and immutable path binding.

    Reject cycles or conflicting knowledge paths before commit; this operation
    does not initialise or write knowledge files."""
    existing = _context_or_error(session, context_id)
    new_parent_context_id = (
        existing.parent_context_id if parent_context_id is UNSET else parent_context_id
    )
    new_parent_context_id = _validate_context_parent(
        session, context_id=existing.id, parent_context_id=new_parent_context_id
    )
    # Massgeblich ist der DB-Stand, nicht ein zwischengespeichertes Attribut: nur so
    # kann dieser PATCH keine Bindung ueberschreiben, die eine andere Session
    # inzwischen gesetzt hat.
    committed_knowledge_path = _committed_knowledge_path(session, existing.id)
    new_knowledge_path = (
        committed_knowledge_path
        if knowledge_path is UNSET
        else _normalized_knowledge_path(knowledge_path)
    )
    if new_knowledge_path != committed_knowledge_path and committed_knowledge_path is not None:
        # Gebunden heisst dauerhaft. Titel- und Parent-Aenderungen verschieben
        # keine Dateien, und ein spaeterer Pfadwechsel wuerde den bereits
        # geschriebenen Markdown-Inhalt von seinem Verweis trennen.
        raise ConflictError(
            "knowledge_path ist bereits gebunden und kann nicht geaendert oder geloescht werden"
        )
    new_knowledge_path = _assert_knowledge_path_available(
        session, new_knowledge_path, context_id=existing.id
    )
    changes = _changes(
        title=(existing.title, _required_title(title) if title is not UNSET else existing.title),
        description=(
            existing.description,
            description if description is not UNSET else existing.description,
        ),
        type=(existing.type, _required_type(type) if type is not UNSET else existing.type),
        parent_context_id=(existing.parent_context_id, new_parent_context_id),
        knowledge_path=(committed_knowledge_path, new_knowledge_path),
    )
    if not changes:
        if committed_knowledge_path != existing.knowledge_path:
            # Ein zwischengespeicherter Stand darf nicht zurueckbleiben: sonst
            # meldet derselbe Aufruf einen anderen Wert zurueck als die Datenbank.
            session.refresh(existing, attribute_names=["knowledge_path"])
        return existing
    for field, change in changes.items():
        setattr(existing, field, change["new"])
    record_event(
        session,
        event_type="CONTEXT_UPDATED",
        entity_type="context",
        entity_id=existing.id,
        payload={"changes": changes},
        context=context,
    )
    _commit(session)
    return existing


def set_context_status(
    session: Session, *, context_id: str, status: str, context: EventContext | None = None
) -> Context:
    """Apply a permitted context lifecycle transition and record its event."""
    existing = _context_or_error(session, context_id)
    status = _validate_context_status(status)
    if existing.status == status:
        return existing
    if status not in CONTEXT_STATUS_TRANSITIONS[existing.status]:
        raise ConflictError(f"Context kann nicht von {existing.status} nach {status} wechseln")
    old_status = existing.status
    existing.status = status
    record_event(
        session,
        event_type="CONTEXT_STATUS_CHANGED",
        entity_type="context",
        entity_id=existing.id,
        payload={"changes": _changes(status=(old_status, status))},
        context=context,
    )
    _commit(session)
    return existing


def get_context(session: Session, context_id: str) -> Context:
    """Return one context or raise NotFoundError; never scan its sources."""
    return _context_or_error(session, context_id)


def list_contexts(
    session: Session,
    *,
    status: str | None = None,
    type: str | None = None,
    parent_context_id: str | None = None,
    roots_only: bool = False,
) -> list[Context]:
    """Read contexts using explicit lifecycle, type and parent/root filters."""
    statement: Select[tuple[Context]] = select(Context).order_by(Context.created_at, Context.id)
    if status is not None:
        statement = statement.where(Context.status == _validate_context_status(status))
    if type is not None:
        statement = statement.where(Context.type == type)
    if parent_context_id is not None:
        _context_or_error(session, parent_context_id)
        statement = statement.where(Context.parent_context_id == parent_context_id)
    if roots_only:
        statement = statement.where(Context.parent_context_id.is_(None))
    return list(session.scalars(statement))


def list_child_contexts(session: Session, parent_context_id: str) -> list[Context]:
    """Read immediate children of an existing context without recursive aggregation."""
    _context_or_error(session, parent_context_id)
    return list_contexts(session, parent_context_id=parent_context_id)
