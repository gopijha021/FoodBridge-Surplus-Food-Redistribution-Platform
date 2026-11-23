import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core import locations
from app.core.config import settings
from app.core.deps import is_member, require_member
from app.core.errors import AppError
from app.db.models import Allocation, DeliveryTask, Donation, User, VolunteerLocation, utcnow
from app.db.outbox import emit
from app.domain.enums import AllocationStatus as A
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType
from app.domain.enums import TaskStatus as T
from app.domain.state import transition
from app.modules.allocations.service import get_allocation_or_404
from app.modules.donations.service import get_donation_or_404, now_utc
from app.modules.matching.geo import haversine_km

BUSY = (T.OFFERED, T.ACCEPTED, T.PICKED_UP)


def get_task_or_404(db: Session, task_id: uuid.UUID) -> DeliveryTask:
    t = db.get(DeliveryTask, task_id)
    if not t:
        raise AppError("TASK_NOT_FOUND", "Task not found.", 404)
    return t


def lock_chain(db: Session, task_id: uuid.UUID) -> tuple[DeliveryTask, Allocation, Donation]:
    """Always lock donation -> allocation -> task, in that order, so concurrent flows cannot deadlock."""
    task = get_task_or_404(db, task_id)
    alloc0 = db.get(Allocation, task.allocation_id)
    donation = get_donation_or_404(db, alloc0.donation_id, lock=True)
    alloc = get_allocation_or_404(db, alloc0.id, lock=True)
    task = db.scalar(select(DeliveryTask).where(DeliveryTask.id == task_id).with_for_update().execution_options(populate_existing=True))
    return task, alloc, donation


def can_view_task(db: Session, user: User, task: DeliveryTask, alloc: Allocation, donation: Donation) -> bool:
    if user.role == "ADMIN" or task.volunteer_id == user.id:
        return True
    return is_member(db, user, alloc.ngo_org_id) or is_member(db, user, donation.donor_org_id)


def record_location(db: Session, user: User, lat: float, lon: float) -> None:
    locations.set_latest(str(user.id), lat, lon)
    last = db.scalar(select(func.max(VolunteerLocation.recorded_at)).where(VolunteerLocation.volunteer_id == user.id))
    if last is None or (utcnow() - last) > timedelta(seconds=60):  # periodic snapshot, not every GPS point
        db.add(VolunteerLocation(volunteer_id=user.id, latitude=lat, longitude=lon))
        db.commit()


def find_nearest_volunteer(db: Session, donation: Donation, task: DeliveryTask, now: datetime) -> uuid.UUID | None:
    cutoff = now - timedelta(minutes=settings.volunteer_location_max_age_minutes)
    latest = (select(VolunteerLocation.volunteer_id.label("vid"), func.max(VolunteerLocation.recorded_at).label("t"))
              .where(VolunteerLocation.recorded_at >= cutoff).group_by(VolunteerLocation.volunteer_id).subquery())
    rows = db.scalars(select(VolunteerLocation).join(
        latest, and_(VolunteerLocation.volunteer_id == latest.c.vid, VolunteerLocation.recorded_at == latest.c.t))).all()
    busy = set(db.scalars(select(DeliveryTask.volunteer_id).where(DeliveryTask.status.in_(BUSY), DeliveryTask.volunteer_id.is_not(None))))
    declined = {str(x) for x in (task.declined_volunteer_ids or [])}
    best, best_d = None, None
    for loc in rows:
        if loc.volunteer_id in busy or str(loc.volunteer_id) in declined:
            continue
        u = db.get(User, loc.volunteer_id)
        if not u or not u.is_active or u.role != "VOLUNTEER":
            continue
        dist = haversine_km(donation.latitude, donation.longitude, loc.latitude, loc.longitude)
        if dist <= settings.volunteer_search_radius_km and (best_d is None or dist < best_d):
            best, best_d = loc.volunteer_id, dist
    return best


def _offer(db: Session, task: DeliveryTask, alloc: Allocation, donation: Donation, volunteer_id: uuid.UUID, actor_id):
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.OFFERED, actor_id=actor_id,
               reason=f"offered to {volunteer_id}")
    task.volunteer_id, task.offered_at = volunteer_id, utcnow()
    emit(db, "task", task.id, "VolunteerAssigned", {"task_id": str(task.id), "volunteer_id": str(volunteer_id),
         "donation_id": str(donation.id), "allocation_id": str(alloc.id)})


def assign_volunteer(db: Session, actor: User, allocation_id: uuid.UUID, volunteer_id: uuid.UUID | None) -> DeliveryTask:
    alloc = get_allocation_or_404(db, allocation_id)
    require_member(db, actor, alloc.ngo_org_id, "NGO")
    task_id = db.scalar(select(DeliveryTask.id).where(DeliveryTask.allocation_id == allocation_id))
    task, alloc, donation = lock_chain(db, task_id)
    if alloc.status != A.CONFIRMED or task.status != T.OPEN:
        raise AppError("INVALID_STATE_TRANSITION", f"Task is {task.status}; only OPEN tasks can be assigned.", 409, {"from": task.status})
    if volunteer_id:
        v = db.get(User, volunteer_id)
        if not v or v.role != "VOLUNTEER" or not v.is_active:
            raise AppError("VOLUNTEER_NOT_FOUND", "Volunteer not found.", 404)
    else:
        volunteer_id = find_nearest_volunteer(db, donation, task, now_utc())
        if not volunteer_id:
            raise AppError("NO_VOLUNTEER_AVAILABLE", "No eligible volunteer is available nearby right now.", 404)
    _offer(db, task, alloc, donation, volunteer_id, actor.id)
    db.commit()
    return task


def auto_assign(db: Session, task_id: uuid.UUID) -> bool:
    """System-driven assignment (event consumer / reconciliation). Does not touch the NGO allocation."""
    task, alloc, donation = lock_chain(db, task_id)
    if alloc.status != A.CONFIRMED or task.status != T.OPEN:
        db.rollback()
        return False
    vid = find_nearest_volunteer(db, donation, task, now_utc())
    if not vid:
        db.rollback()
        return False
    _offer(db, task, alloc, donation, vid, None)
    db.commit()
    return True


def accept(db: Session, user: User, task_id: uuid.UUID) -> DeliveryTask:
    task, alloc, donation = lock_chain(db, task_id)
    if task.status == T.OFFERED and task.volunteer_id != user.id:
        raise AppError("TASK_NOT_AVAILABLE", "This task was offered to another volunteer.", 409)
    if task.status not in (T.OFFERED, T.OPEN):
        raise AppError("TASK_NOT_AVAILABLE", f"Task is {task.status}.", 409, {"status": task.status})
    if str(user.id) in {str(x) for x in (task.declined_volunteer_ids or [])}:
        raise AppError("TASK_NOT_AVAILABLE", "You declined this task earlier.", 409)
    if alloc.status != A.CONFIRMED:
        raise AppError("TASK_NOT_AVAILABLE", "Allocation is no longer active.", 409)
    now = now_utc()
    if donation.expires_at <= now:
        raise AppError("DONATION_EXPIRED", "The food has passed its expiry time.", 409)
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.ACCEPTED, actor_id=user.id, reason="accepted")
    task.volunteer_id, task.accepted_at = user.id, utcnow()
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.ASSIGNED, actor_id=user.id, reason="volunteer accepted")
    emit(db, "task", task.id, "TaskAccepted", {"task_id": str(task.id), "donation_id": str(donation.id),
         "volunteer_id": str(user.id), "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return task


def _reset_to_open(task: DeliveryTask, user_id):
    declined = list(task.declined_volunteer_ids or [])
    if user_id and str(user_id) not in declined:
        declined.append(str(user_id))
    task.declined_volunteer_ids = declined
    task.volunteer_id, task.offered_at, task.accepted_at = None, None, None


def decline(db: Session, user: User | None, task_id: uuid.UUID, timed_out: bool = False) -> DeliveryTask:
    """Volunteer declines (or times out). The NGO allocation is untouched; the task returns to OPEN."""
    task, alloc, donation = lock_chain(db, task_id)
    if task.status != T.OFFERED or (user and task.volunteer_id != user.id):
        raise AppError("INVALID_STATE_TRANSITION", "Only the offered volunteer can decline an OFFERED task.", 409, {"from": task.status})
    vid = task.volunteer_id
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.OPEN, actor_id=user.id if user else None,
               reason="offer timed out" if timed_out else "declined")
    _reset_to_open(task, vid)
    emit(db, "task", task.id, "TaskDeclined", {"task_id": str(task.id), "volunteer_id": str(vid), "timed_out": timed_out})
    db.commit()
    return task


def release(db: Session, user: User, task_id: uuid.UUID, reason: str | None) -> DeliveryTask:
    """An accepted volunteer drops out: donation -> REASSIGNMENT_REQUIRED, task -> OPEN."""
    task, alloc, donation = lock_chain(db, task_id)
    if task.volunteer_id != user.id:
        raise AppError("FORBIDDEN_TASK", "This task is not assigned to you.", 403)
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.OPEN, actor_id=user.id, reason=reason or "volunteer released task")
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.REASSIGNMENT_REQUIRED, actor_id=user.id, reason=reason)
    _reset_to_open(task, user.id)
    emit(db, "task", task.id, "TaskDeclined", {"task_id": str(task.id), "volunteer_id": str(user.id), "timed_out": False})
    db.commit()
    return task


def _own_task(task: DeliveryTask, user: User):
    if task.volunteer_id != user.id:
        raise AppError("FORBIDDEN_TASK", "This task is not assigned to you.", 403)


def pickup(db: Session, user: User, task_id: uuid.UUID) -> DeliveryTask:
    task, alloc, donation = lock_chain(db, task_id)
    _own_task(task, user)
    if donation.expires_at <= now_utc():
        raise AppError("DONATION_EXPIRED", "Food has passed its expiry time and must not be collected.", 409)
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.PICKED_UP, actor_id=user.id, reason="pickup confirmed")
    task.pickup_at = utcnow()
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.PICKED_UP, actor_id=user.id, reason="pickup confirmed")
    emit(db, "task", task.id, "PickupConfirmed", {"task_id": str(task.id), "donation_id": str(donation.id),
         "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return task


def deliver(db: Session, user: User, task_id: uuid.UUID) -> DeliveryTask:
    task, alloc, donation = lock_chain(db, task_id)
    _own_task(task, user)
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.DELIVERED, actor_id=user.id, reason="handoff reported")
    task.delivered_at = utcnow()
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.DELIVERED, actor_id=user.id, reason="handoff reported")
    emit(db, "task", task.id, "DeliveryReported", {"task_id": str(task.id), "donation_id": str(donation.id),
         "allocation_id": str(alloc.id), "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return task


def fail(db: Session, user: User, task_id: uuid.UUID, reason: str) -> DeliveryTask:
    task, alloc, donation = lock_chain(db, task_id)
    _own_task(task, user)
    transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.FAILED, actor_id=user.id, reason=reason)
    task.failure_reason = reason
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.DELIVERY_FAILED, actor_id=user.id, reason=reason)
    emit(db, "task", task.id, "DeliveryFailed", {"task_id": str(task.id), "donation_id": str(donation.id), "reason": reason,
         "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return task


def open_tasks_for_volunteer(db: Session, user: User, lat: float, lon: float, radius_km: float) -> list[dict]:
    rows = db.execute(
        select(DeliveryTask, Donation).join(Allocation, Allocation.id == DeliveryTask.allocation_id)
        .join(Donation, Donation.id == Allocation.donation_id)
        .where(DeliveryTask.status == T.OPEN, Allocation.status == A.CONFIRMED,
               Donation.status.in_([D.RESERVED, D.REASSIGNMENT_REQUIRED]), Donation.expires_at > now_utc())
    ).all()
    out = []
    for task, d in rows:
        if str(user.id) in {str(x) for x in (task.declined_volunteer_ids or [])}:
            continue
        dist = haversine_km(lat, lon, d.latitude, d.longitude)
        if dist <= radius_km:
            out.append({"task_id": str(task.id), "title": d.title, "quantity": d.quantity, "unit": d.unit,
                        "pickup_start": d.pickup_start, "pickup_end": d.pickup_end, "distance_km": round(dist, 2),
                        "approx_location": {"latitude": round(d.latitude, 2), "longitude": round(d.longitude, 2)}})
    out.sort(key=lambda r: r["distance_km"])
    return out
