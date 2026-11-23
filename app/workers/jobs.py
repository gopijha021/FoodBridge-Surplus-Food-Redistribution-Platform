"""Scheduled jobs: expiry, volunteer-offer timeouts, reconciliation."""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Allocation, DeliveryTask, Donation, OutboxEvent
from app.db.outbox import emit
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType
from app.domain.state import transition
from app.modules.delivery.service import auto_assign, decline
from app.workers.consumers import deliver_pending_notifications, refresh_matching

log = logging.getLogger("foodbridge.jobs")


def expire_donations(db: Session, now: datetime | None = None, batch: int = 100) -> int:
    """PUBLISHED listings past expires_at -> EXPIRED. SKIP LOCKED lets several workers run safely."""
    now = now or datetime.now(timezone.utc)
    rows = db.scalars(select(Donation).where(Donation.status == D.PUBLISHED, Donation.expires_at <= now)
                      .order_by(Donation.expires_at).limit(batch).with_for_update(skip_locked=True)).all()
    for d in rows:
        transition(db, entity_type=EntityType.DONATION, entity=d, to_status=D.EXPIRED, actor_id=None, reason="expired by scheduler")
        emit(db, "donation", d.id, "DonationExpired", {"donation_id": str(d.id), "donor_org_id": str(d.donor_org_id)})
    db.commit()
    return len(rows)


def _event_exists(db: Session, aggregate_id, event_type: str) -> bool:
    return db.scalar(select(OutboxEvent.id).where(OutboxEvent.aggregate_id == aggregate_id, OutboxEvent.event_type == event_type).limit(1)) is not None


def expire_volunteer_offers(db: Session, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=settings.volunteer_offer_timeout_minutes)
    ids = list(db.scalars(select(DeliveryTask.id).where(DeliveryTask.status == "OFFERED", DeliveryTask.offered_at <= cutoff)))
    n = 0
    for tid in ids:
        try:
            decline(db, None, tid, timed_out=True)
            n += 1
        except Exception:
            db.rollback()
            log.exception("offer timeout handling failed")
    return n


def reconcile(db: Session, now: datetime | None = None) -> dict:
    """Detect stuck work: unassigned tasks, unconfirmed deliveries, missed pickup deadlines, unmatched listings."""
    now = now or datetime.now(timezone.utc)
    stats = {"offers_timed_out": expire_volunteer_offers(db, now), "reassigned": 0, "receipt_overdue": 0, "pickup_missed": 0, "rematched": 0}

    for tid in list(db.scalars(select(DeliveryTask.id).where(DeliveryTask.status == "OPEN"))):
        if auto_assign(db, tid):
            stats["reassigned"] += 1

    overdue = now - timedelta(hours=settings.receipt_confirmation_timeout_hours)
    for d, alloc in db.execute(select(Donation, Allocation).join(Allocation, Allocation.donation_id == Donation.id)
                               .where(Donation.status == D.DELIVERED, Allocation.status == "CONFIRMED")).all():
        task = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == alloc.id))
        if task and task.delivered_at and task.delivered_at <= overdue and not _event_exists(db, alloc.id, "ReceiptOverdue"):
            emit(db, "allocation", alloc.id, "ReceiptOverdue", {"allocation_id": str(alloc.id), "donation_id": str(d.id),
                 "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(d.donor_org_id)})
            stats["receipt_overdue"] += 1

    for d in db.scalars(select(Donation).where(Donation.status.in_([D.RESERVED, D.ASSIGNED, D.REASSIGNMENT_REQUIRED]), Donation.pickup_end <= now)):
        if not _event_exists(db, d.id, "PickupDeadlineMissed"):
            alloc = db.scalar(select(Allocation).where(Allocation.donation_id == d.id, Allocation.status == "CONFIRMED"))
            emit(db, "donation", d.id, "PickupDeadlineMissed", {"donation_id": str(d.id), "donor_org_id": str(d.donor_org_id),
                 "ngo_org_id": str(alloc.ngo_org_id) if alloc else None})
            stats["pickup_missed"] += 1
    db.commit()

    for did in list(db.scalars(select(Donation.id).where(Donation.status == D.PUBLISHED, Donation.expires_at > now).limit(200))):
        stats["rematched"] += refresh_matching(db, did)  # widened radius may now reach more NGOs; dedupe prevents repeats
    db.commit()
    deliver_pending_notifications(db)
    return stats
