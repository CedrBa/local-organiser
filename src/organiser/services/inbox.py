from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from organiser.db.models import Inbox


def store_inbox_item(
    session: Session,
    *,
    item_id: str,
    created_at: str,
    text: str,
    raw_payload: dict,
) -> bool:
    """Store an inbox item once; an external inbox ID makes retries safe."""
    item = Inbox(
        id=item_id,
        created_at=created_at,
        received_at=datetime.now(UTC),
        text=text,
        source="api",
        raw_payload=raw_payload,
        processing_status="pending",
    )
    session.add(item)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return False
    return True
