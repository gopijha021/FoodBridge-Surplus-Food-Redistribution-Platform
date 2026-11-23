import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import member_org_ids
from app.core.errors import AppError
from app.db.models import Allocation, DeliveryTask, Donation, FoodDetails, Organisation, User
from app.db.outbox import emit
from app.domain.enums import AllocationStatus as A
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType, OrgType
from app.domain.enums import TaskStatus as T
from app.domain.state import can_transition, transition
from app.modules.donations.schemas import DonationCreateIn, DonationOut


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def resolve_donor_org(db: Session, user: User, requested: uuid.UUID | None) -> Organisation:
    ids = member_org_ids(db, user.id)
    orgs = db.scalars(select(Organisation).where(Organisation.id.in_(ids), Organisation.type == OrgType.DONOR)).all() if ids else []
    if requested:
        org = next((o for o in orgs if o.id == requested), None)
        if not org:
            raise AppError("NOT_ORGANISATION_MEMBER", "You are not a member of that donor organisation.", 403)
    elif len(orgs) == 1:
        org = orgs[0]
    elif not orgs:
        raise AppError("NO_DONOR_ORGANISATION", "Create a donor organisation first.", 422)
    else:
        raise AppError("DONOR_ORG_REQUIRED", "Specify donor_org_id.", 422)
    if not org.is_active or org.is_blocked:
        raise AppError("ORGANISATION_NOT_ELIGIBLE", "Organisation is suspended or inactive.", 403)
    return org


def create_draft(db: Session, user: User, body: DonationCreateIn) -> Donation:
    org = resolve_donor_org(db, user, body.donor_org_id)
    d = Donation(
        donor_org_id=org.id, created_by=user.id, title=body.title, category=body.category, quantity=body.quantity,
        unit=body.unit, prepared_at=body.prepared_at, expires_at=body.expires_at, pickup_start=body.pickup_start,
        pickup_end=body.pickup_end, latitude=body.location.latitude, longitude=body.location.longitude,
        status=D.DRAFT.value,
    )
    db.add(d)
    db.flush()
    db.add(FoodDetails(donation_id=d.id, storage_type=body.storage_type, packaging=body.packaging,
                       allergen_notes=body.allergen_notes, dietary_tags=body.dietary_tags,
                       handling_notes=body.handling_notes))
    from app.domain.state import record_history

    record_history(db, EntityType.DONATION, d.id, None, D.DRAFT.value, user.id, "created")
    db.commit()
    return d


def get_donation_or_404(db: Session, donation_id: uuid.UUID, lock: bool = False) -> Donation:
    stmt = select(Donation).where(Donation.id == donation_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    d = db.scalar(stmt)
    if not d:
        raise AppError("DONATION_NOT_FOUND", "Donation not found.", 404)
    return d


def active_allocation(db: Session, donation_id: uuid.UUID) -> Allocation | None:
    return db.scalar(select(Allocation).where(Allocation.donation_id == donation_id,
                                               Allocation.status.in_([A.CONFIRMED, A.DISPUTED, A.COMPLETED])))


def can_see_exact(db: Session, user: User, d: Donation) -> bool:
    if user.role == "ADMIN" or d.donor_org_id in member_org_ids(db, user.id):
        return True
    alloc = active_allocation(db, d.id)
    if not alloc:
        return False
    if user.role == "NGO" and alloc.ngo_org_id in member_org_ids(db, user.id):
        return True
    if user.role == "VOLUNTEER":
        task = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == alloc.id))
        return bool(task and task.volunteer_id == user.id and task.status in (T.ACCEPTED, T.PICKED_UP, T.DELIVERED))
    return False


def present(db: Session, d: Donation, exact: bool) -> DonationOut:
    food = db.get(FoodDetails, d.id)
    # Privacy: unauthorised viewers only get ~1 km precision until they are part of the delivery.
    loc = ({"latitude": d.latitude, "longitude": d.longitude, "precision": "exact"} if exact else
           {"latitude": round(d.latitude, 2), "longitude": round(d.longitude, 2), "precision": "approximate"})
    return DonationOut(
        id=str(d.id), donor_org_id=str(d.donor_org_id), title=d.title, category=d.category, quantity=d.quantity,
        unit=d.unit, prepared_at=d.prepared_at, expires_at=d.expires_at, pickup_start=d.pickup_start,
        pickup_end=d.pickup_end, location=loc, status=d.status, version=d.version,
        storage_type=food.storage_type if food else None, packaging=food.packaging if food else None,
        allergen_notes=food.allergen_notes if food else None, dietary_tags=food.dietary_tags if food else None,
        handling_notes=food.handling_notes if food else None,
    )


def publish(db: Session, d: Donation, actor: User) -> Donation:
    now = now_utc()
    if d.expires_at <= now or d.pickup_end <= now:
        raise AppError("DONATION_ALREADY_EXPIRED", "Expiry or pickup window is already in the past.", 422)
    transition(db, entity_type=EntityType.DONATION, entity=d, to_status=D.PUBLISHED, actor_id=actor.id, reason="published")
    d.published_at = now
    emit(db, "donation", d.id, "DonationPublished", {"donation_id": str(d.id)})
    db.commit()
    return d


def withdraw(db: Session, d: Donation, actor: User, reason: str | None) -> Donation:
    transition(db, entity_type=EntityType.DONATION, entity=d, to_status=D.WITHDRAWN, actor_id=actor.id, reason=reason)
    emit(db, "donation", d.id, "DonationWithdrawn", {"donation_id": str(d.id)})
    db.commit()
    return d


def cancel(db: Session, d: Donation, actor: User, reason: str | None) -> Donation:
    """Cancel a donation and cascade to its live allocation / delivery task."""
    if not can_transition(EntityType.DONATION, d.status, D.CANCELLED):
        raise AppError("INVALID_STATE_TRANSITION", f"Cannot cancel a donation in status {d.status}.", 409,
                       {"from": d.status, "to": "CANCELLED"})
    alloc = db.scalar(select(Allocation).where(Allocation.donation_id == d.id,
                                               Allocation.status.in_([A.CONFIRMED, A.DISPUTED])).with_for_update())
    transition(db, entity_type=EntityType.DONATION, entity=d, to_status=D.CANCELLED, actor_id=actor.id, reason=reason)
    if alloc:
        task = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == alloc.id).with_for_update())
        if task and can_transition(EntityType.TASK, task.status, T.CANCELLED):
            transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.CANCELLED, actor_id=actor.id, reason="donation cancelled")
        transition(db, entity_type=EntityType.ALLOCATION, entity=alloc, to_status=A.CANCELLED, actor_id=actor.id, reason="donation cancelled")
    emit(db, "donation", d.id, "DonationCancelled", {"donation_id": str(d.id), "ngo_org_id": str(alloc.ngo_org_id) if alloc else None})
    db.commit()
    return d
