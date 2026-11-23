import logging

from celery import Celery

from app.core.config import settings

log = logging.getLogger("foodbridge.celery")
broker = settings.broker_url or settings.redis_url or "memory://"
celery_app = Celery("foodbridge", broker=broker, backend=None)
celery_app.conf.update(
    task_acks_late=True, task_reject_on_worker_lost=True, broker_connection_retry_on_startup=True,
    broker_transport_options={"socket_timeout": 5}, timezone="UTC", enable_utc=True,
    beat_schedule={
        "publish-outbox": {"task": "foodbridge.publish_outbox", "schedule": 5.0},
        "expire-donations": {"task": "foodbridge.expire_donations", "schedule": 60.0},
        "reconcile": {"task": "foodbridge.reconcile", "schedule": 300.0},
    },
)


@celery_app.task(name="foodbridge.publish_outbox")
def publish_outbox_task() -> int:
    from app.db.session import SessionLocal
    from app.workers.outbox import publish_pending

    with SessionLocal() as db:
        return publish_pending(db)


@celery_app.task(name="foodbridge.expire_donations")
def expire_donations_task() -> int:
    from app.db.session import SessionLocal
    from app.workers.jobs import expire_donations

    with SessionLocal() as db:
        return expire_donations(db)


@celery_app.task(name="foodbridge.reconcile")
def reconcile_task() -> dict:
    from app.db.session import SessionLocal
    from app.workers.jobs import reconcile

    with SessionLocal() as db:
        return reconcile(db)


@celery_app.task(
    bind=True, name="foodbridge.handle_event", autoretry_for=(Exception,), retry_backoff=True, retry_backoff_max=300,
    retry_jitter=True, max_retries=5,
)
def handle_event(self, ev: dict) -> None:
    """Exponential backoff with jitter; after the last retry the event goes to the dead-letter table."""
    from app.db.session import SessionLocal
    from app.workers.consumers import dispatch, record_dead_letter

    try:
        with SessionLocal() as db:
            dispatch(db, ev)
    except Exception as exc:
        if self.request.retries >= self.max_retries:
            with SessionLocal() as db:
                record_dead_letter(db, ev, repr(exc))
            log.error("event dead-lettered", extra={"extra_fields": {"event_id": ev.get("id")}})
        raise
