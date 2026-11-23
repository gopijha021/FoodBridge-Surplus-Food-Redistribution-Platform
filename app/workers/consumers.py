"""Event consumers. Every consumer is idempotent: dispatch() records (event_id, consumer) in the same
transaction as the consumer's effects, and notifications carry a unique dedupe_key."""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import (
    DeadLetterEvent, DeliveryTask, Donation, Notification, OrganisationMembership, OutboxEvent, ProcessedEvent, User,
    utcnow,
)
from app.integrations import notifier
from app.modules.delivery.service import auto_assign
from app.modules.matching.service import rank_candidates

log = logging.getLogger("foodbridge.consumers")


def org_user_ids(db: Session, org_id) -> list[uuid.UUID]:
    if not org_id:
        return []
    return list(db.scalars(select(OrganisationMembership.user_id).where(OrganisationMembership.organisation_id == uuid.UUID(str(org_id)))))


def admin_ids(db: Session) -> list[uuid.UUID]:
    return list(db.scalars(select(User.id).where(User.role == "ADMIN", User.is_active.is_(True))))


def notify(db: Session, user_ids, event_type: str, payload: dict, key: str) -> int:
    n = 0
    for uid in set(user_ids):
        try:
            with db.begin_nested():  # savepoint: a duplicate dedupe_key must not poison the transaction
                db.add(Notification(user_id=uid, event_type=event_type, payload=payload, dedupe_key=f"{event_type}:{key}:{uid}"))
                db.flush()
            n += 1
        except IntegrityError:
            pass
    return n


def deliver_pending_notifications(db: Session, batch: int = 100) -> int:
    rows = db.scalars(select(Notification).where(Notification.status == "PENDING").order_by(Notification.created_at)
                      .limit(batch).with_for_update(skip_locked=True)).all()
    sent = 0
    for n in rows:
        user = db.get(User, n.user_id)
        try:
            notifier.send(user.email, user.phone, n.event_type, n.payload)
            n.status, n.sent_at = "SENT", utcnow()
            sent += 1
        except Exception:
            n.retry_count += 1
            if n.retry_count >= settings.notification_max_retries:
                n.status = "FAILED"
            log.exception("notification delivery failed")
    db.commit()
    return sent


# --- matching -----------------------------------------------------------------------------------------------

def refresh_matching(db: Session, donation_id: uuid.UUID) -> int:
    """Notify eligible NGOs (radius widens with listing age). No NGO found -> keep published, alert admin after a while."""
    d = db.get(Donation, donation_id)
    if not d or d.status != "PUBLISHED":
        return 0
    now = datetime.now(timezone.utc)
    cands = rank_candidates(db, d, now)
    count = 0
    for c in cands:
        count += notify(db, org_user_ids(db, c["ngo_org_id"]), "DonationAvailable",
                        {"donation_id": str(d.id), "title": d.title, "score": c["score"], "distance_km": c["distance_km"]}, str(d.id))
    age = now - (d.published_at or d.created_at)
    if not cands and age >= timedelta(minutes=settings.admin_alert_after_minutes):
        count += notify(db, admin_ids(db), "NoNgoFound", {"donation_id": str(d.id), "title": d.title}, str(d.id))
    return count


def c_matching(db: Session, ev: dict) -> None:
    if ev["event_type"] == "DonationPublished":
        refresh_matching(db, uuid.UUID(ev["payload"]["donation_id"]))


# --- assignment ---------------------------------------------------------------------------------------------

def c_assignment(db: Session, ev: dict) -> None:
    p = ev["payload"]
    if ev["event_type"] == "AllocationConfirmed":
        auto_assign(db, uuid.UUID(p["task_id"]))
    elif ev["event_type"] == "TaskDeclined":
        auto_assign(db, uuid.UUID(p["task_id"]))  # find another volunteer; NGO allocation unchanged


# --- notifications ------------------------------------------------------------------------------------------

def c_notifications(db: Session, ev: dict) -> None:
    t, p, eid = ev["event_type"], ev["payload"], ev["id"]
    ngo, donor = org_user_ids(db, p.get("ngo_org_id")), org_user_ids(db, p.get("donor_org_id"))
    if t == "AllocationConfirmed":
        notify(db, ngo + donor, t, p, eid)
    elif t == "VolunteerAssigned":
        notify(db, [uuid.UUID(p["volunteer_id"])], t, p, eid)
    elif t == "TaskAccepted":
        notify(db, ngo + donor, t, p, eid)
    elif t == "PickupConfirmed":
        notify(db, ngo, t, p, eid)
    elif t == "DeliveryReported":
        notify(db, ngo, "ConfirmReceiptRequested", p, eid)
    elif t == "ReceiptConfirmed":
        notify(db, ngo + donor, t, p, eid)
    elif t in ("DeliveryDisputed", "DeliveryFailed"):
        notify(db, ngo + donor + admin_ids(db), t, p, eid)
    elif t in ("DonationExpired", "DonationCancelled", "DonationWithdrawn"):
        notify(db, donor + ngo, t, p, eid)
    elif t == "ReceiptOverdue":
        notify(db, ngo + admin_ids(db), t, p, eid)
    elif t == "PickupDeadlineMissed":
        notify(db, ngo + donor + admin_ids(db), t, p, eid)
    elif t == "VerificationSubmitted":
        notify(db, admin_ids(db), t, p, eid)
    elif t in ("OrganisationVerified", "OrganisationRejected"):
        notify(db, org_user_ids(db, p.get("org_id")), t, p, eid)
    deliver_pending_notifications(db)


# --- analytics / impact -------------------------------------------------------------------------------------

def c_analytics(db: Session, ev: dict) -> None:
    # Impact rows are written transactionally at confirmation time; this hook is where dashboards/aggregates refresh.
    log.info("analytics event", extra={"extra_fields": {"event_type": ev["event_type"]}})


CONSUMERS = {
    "matching": c_matching, "assignment": c_assignment, "notifications": c_notifications, "analytics": c_analytics,
}


def dispatch(db: Session, ev: dict) -> None:
    """Run every consumer once per event. Re-delivery of the same event is a no-op."""
    event_id = uuid.UUID(ev["id"])
    for name, fn in CONSUMERS.items():
        if db.get(ProcessedEvent, (event_id, name)):
            continue
        try:
            fn(db, ev)
            db.add(ProcessedEvent(event_id=event_id, consumer=name))
            db.commit()
        except IntegrityError:  # another worker processed it concurrently
            db.rollback()
        except Exception:
            db.rollback()
            raise


def record_dead_letter(db: Session, ev: dict, error: str) -> None:
    db.add(DeadLetterEvent(event_id=uuid.UUID(ev["id"]), event_type=ev.get("event_type"), payload=ev, error=error[:4000]))
    db.commit()
