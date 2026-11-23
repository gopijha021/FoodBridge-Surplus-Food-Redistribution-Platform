"""Outbox publisher: sends committed-but-unpublished events to the broker, then marks them published.
At-least-once: a crash between send and mark re-sends, so consumers must be idempotent (they are)."""
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import OutboxEvent, utcnow

log = logging.getLogger("foodbridge.outbox")


def event_to_dict(e: OutboxEvent) -> dict:
    return {"id": str(e.id), "aggregate_type": e.aggregate_type, "aggregate_id": str(e.aggregate_id),
            "event_type": e.event_type, "payload": e.payload, "created_at": e.created_at.isoformat()}


def publish_event(ev: dict) -> None:
    """Transport. `celery` -> broker (RabbitMQ/Redis); `inline` -> handle in-process (tests / single-process dev)."""
    if settings.event_backend == "celery":
        from app.workers.celery_app import celery_app

        celery_app.send_task("foodbridge.handle_event", args=[ev])
    else:
        from app.db.session import SessionLocal
        from app.workers.consumers import dispatch

        with SessionLocal() as db:
            dispatch(db, ev)


def publish_pending(db: Session, batch: int = 100) -> int:
    rows = db.scalars(select(OutboxEvent).where(OutboxEvent.published_at.is_(None)).order_by(OutboxEvent.created_at)
                      .limit(batch).with_for_update(skip_locked=True)).all()
    published = 0
    for e in rows:
        try:
            publish_event(event_to_dict(e))
        except Exception:
            e.attempts += 1
            log.exception("outbox publish failed; will retry", extra={"extra_fields": {"event_id": str(e.id)}})
            break  # preserve ordering: stop at the first failure, retry next tick
        e.published_at = utcnow()
        published += 1
        db.commit()  # commit per event so a later failure never re-sends earlier ones
    db.commit()
    return published
