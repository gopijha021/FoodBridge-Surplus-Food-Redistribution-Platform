import uuid

from sqlalchemy.orm import Session

from app.db.models import OutboxEvent


def emit(db: Session, aggregate_type: str, aggregate_id: uuid.UUID, event_type: str, payload: dict | None = None) -> OutboxEvent:
    """Write the event intent in the SAME transaction as the business change (transactional outbox)."""
    ev = OutboxEvent(aggregate_type=aggregate_type, aggregate_id=aggregate_id, event_type=event_type,
                     payload=payload or {})
    db.add(ev)
    return ev
