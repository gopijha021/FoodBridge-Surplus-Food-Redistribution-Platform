import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.deps import require_member
from app.core.errors import AppError
from app.db.models import Allocation, DeliveryTask, Donation, FoodDetails, ImpactRecord, Organisation, User
from app.db.models import utcnow
from app.db.outbox import emit
from app.domain.enums import AllocationStatus as A
from app.domain.enums import DonationStatus as D
from app.domain.enums import EntityType
from app.domain.enums import TaskStatus as T
from app.domain.state import record_history, transition
from app.modules.donations.service import get_donation_or_404, now_utc
from app.modules.matching.service import check_eligibility
from app.core.config import settings


def _existing_for_key(db: Session, user_id: uuid.UUID, key: str) -> Allocation | None:
    return db.scalar(select(Allocation).where(Allocation.claimed_by == user_id, Allocation.idempotency_key == key))


def claim_donation(db: Session, *, user: User, donation_id: uuid.UUID, ngo_org_id: uuid.UUID, idempotency_key: str):
    """Race-safe allocation: lock donation row -> verify -> insert allocation -> RESERVED -> outbox, one transaction.
    Returns (allocation, created). `created=False` means an idempotent replay of an earlier claim."""
    ngo = require_member(db, user, ngo_org_id, "NGO")
    if ngo.verification_status != "VERIFIED" or not ngo.is_active or ngo.is_blocked:
        raise AppError("ORGANISATION_NOT_ELIGIBLE", "Only verified, active NGOs can claim donations.", 403,
                       {"verification_status": ngo.verification_status})

    prior = _existing_for_key(db, user.id, idempotency_key)
    if prior:
        if prior.donation_id != donation_id:
            raise AppError("IDEMPOTENCY_KEY_REUSED", "This Idempotency-Key was used for a different donation.", 422)
        return prior, False

    donation = get_donation_or_404(db, donation_id, lock=True)  # SELECT ... FOR UPDATE

    # a concurrent request with the same key may have won while we waited for the lock
    prior = _existing_for_key(db, user.id, idempotency_key)
    if prior:
        return prior, False

    now = now_utc()
    if donation.status == D.PUBLISHED and (donation.expires_at <= now or donation.pickup_end <= now):
        raise AppError("DONATION_EXPIRED", "This donation has expired.", 409)
    if donation.status != D.PUBLISHED:
        code = "DONATION_ALREADY_RESERVED" if donation.status not in (D.DRAFT, D.WITHDRAWN, D.EXPIRED, D.CANCELLED) \
            else "DONATION_NOT_AVAILABLE"
        raise AppError(code, "This donation is no longer available.", 409, {"status": donation.status})

    donor_org = db.get(Organisation, donation.donor_org_id)
    elig = check_eligibility(donation, db.get(FoodDetails, donation.id), ngo, donor_org, now)
    if not elig.ok:
        raise AppError("NOT_ELIGIBLE", "Your organisation is not eligible for this donation.", 422, {"reasons": elig.reasons})

    alloc = Allocation(donation_id=donation.id, ngo_org_id=ngo.id, claimed_by=user.id,
                       allocated_quantity=donation.quantity, status=A.CONFIRMED.value, idempotency_key=idempotency_key)
    db.add(alloc)
    try:
        db.flush()  # unique index = second line of defence
    except IntegrityError:
        db.rollback()
        prior = _existing_for_key(db, user.id, idempotency_key)
        if prior and prior.donation_id == donation_id:
            return prior, False
        raise AppError("DONATION_ALREADY_RESERVED", "This donation is no longer available.", 409)

    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.RESERVED, actor_id=user.id,
               reason=f"claimed by {ngo.name}")
    record_history(db, EntityType.ALLOCATION, alloc.id, None, A.CONFIRMED.value, user.id, "claim confirmed")
    task = DeliveryTask(allocation_id=alloc.id, status=T.OPEN.value, declined_volunteer_ids=[])
    db.add(task)
    db.flush()
    record_history(db, EntityType.TASK, task.id, None, T.OPEN.value, user.id, "task created")
    emit(db, "allocation", alloc.id, "AllocationConfirmed", {
        "allocation_id": str(alloc.id), "donation_id": str(donation.id), "task_id": str(task.id),
        "ngo_org_id": str(ngo.id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return alloc, True


def get_allocation_or_404(db: Session, allocation_id: uuid.UUID, lock: bool = False) -> Allocation:
    stmt = select(Allocation).where(Allocation.id == allocation_id)
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    a = db.scalar(stmt)
    if not a:
        raise AppError("ALLOCATION_NOT_FOUND", "Allocation not found.", 404)
    return a


def release_allocation(db: Session, user: User, allocation_id: uuid.UUID, reason: str | None) -> Allocation:
    """NGO gives the donation back before a volunteer has accepted; the listing is re-published."""
    alloc = get_allocation_or_404(db, allocation_id)
    require_member(db, user, alloc.ngo_org_id, "NGO")
    donation = get_donation_or_404(db, alloc.donation_id, lock=True)
    alloc = get_allocation_or_404(db, allocation_id, lock=True)
    task = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == alloc.id).with_for_update())
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.RELEASED, actor_id=user.id, reason=reason)
    transition(db, entity_type=EntityType.ALLOCATION, entity=alloc, to_status=A.RELEASED, actor_id=user.id, reason=reason)
    if task:
        transition(db, entity_type=EntityType.TASK, entity=task, to_status=T.CANCELLED, actor_id=user.id, reason="allocation released")
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.PUBLISHED, actor_id=None, reason="re-listed after release")
    emit(db, "donation", donation.id, "DonationPublished", {"donation_id": str(donation.id), "relisted": True})
    db.commit()
    return alloc


def confirm_receipt(db: Session, user: User, allocation_id: uuid.UUID, received_quantity: float, condition_ok: bool,
                    notes: str | None) -> tuple[Allocation, Donation]:
    alloc = get_allocation_or_404(db, allocation_id)
    require_member(db, user, alloc.ngo_org_id, "NGO")
    donation = get_donation_or_404(db, alloc.donation_id, lock=True)
    alloc = get_allocation_or_404(db, allocation_id, lock=True)
    if donation.status != D.DELIVERED:
        raise AppError("INVALID_STATE_TRANSITION", f"Receipt can only be confirmed for DELIVERED donations (is {donation.status}).",
                       409, {"from": donation.status})
    quantity_ok = received_quantity >= alloc.allocated_quantity * (1 - settings.receipt_quantity_tolerance)
    if condition_ok and quantity_ok:
        _complete(db, donation, alloc, received_quantity, user.id, "NGO_CONFIRMED", notes or "receipt confirmed")
        emit(db, "allocation", alloc.id, "ReceiptConfirmed", {"allocation_id": str(alloc.id), "donation_id": str(donation.id),
             "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    else:
        reason = f"disputed: received {received_quantity} of {alloc.allocated_quantity}; condition_ok={condition_ok}; {notes or ''}".strip()
        transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.DISPUTED, actor_id=user.id, reason=reason)
        transition(db, entity_type=EntityType.ALLOCATION, entity=alloc, to_status=A.DISPUTED, actor_id=user.id, reason=reason)
        emit(db, "allocation", alloc.id, "DeliveryDisputed", {"allocation_id": str(alloc.id), "donation_id": str(donation.id),
             "reason": reason, "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    db.commit()
    return alloc, donation


def _complete(db, donation, alloc, quantity, actor_id, source, reason):
    transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.COMPLETED, actor_id=actor_id, reason=reason)
    transition(db, entity_type=EntityType.ALLOCATION, entity=alloc, to_status=A.COMPLETED, actor_id=actor_id, reason=reason)
    db.add(ImpactRecord(donation_id=donation.id, donor_org_id=donation.donor_org_id, ngo_org_id=alloc.ngo_org_id,
                        quantity_redistributed=min(quantity, alloc.allocated_quantity), unit=donation.unit,
                        verified_at=utcnow(), source=source))


def resolve_dispute(db: Session, admin: User, donation_id: uuid.UUID, outcome: str, quantity: float | None, reason: str):
    donation = get_donation_or_404(db, donation_id, lock=True)
    alloc = db.scalar(select(Allocation).where(Allocation.donation_id == donation.id, Allocation.status == A.DISPUTED).with_for_update())
    if donation.status != D.DISPUTED or not alloc:
        raise AppError("INVALID_STATE_TRANSITION", "Donation is not in DISPUTED state.", 409, {"from": donation.status})
    if outcome == "COMPLETED":
        _complete(db, donation, alloc, quantity if quantity is not None else alloc.allocated_quantity, admin.id,
                  "ADMIN_RESOLVED", reason)
        emit(db, "allocation", alloc.id, "ReceiptConfirmed", {"allocation_id": str(alloc.id), "donation_id": str(donation.id),
             "ngo_org_id": str(alloc.ngo_org_id), "donor_org_id": str(donation.donor_org_id)})
    else:
        transition(db, entity_type=EntityType.DONATION, entity=donation, to_status=D.CANCELLED, actor_id=admin.id, reason=reason)
        transition(db, entity_type=EntityType.ALLOCATION, entity=alloc, to_status=A.CANCELLED, actor_id=admin.id, reason=reason)
        emit(db, "donation", donation.id, "DonationCancelled", {"donation_id": str(donation.id), "ngo_org_id": str(alloc.ngo_org_id)})
    db.commit()
    return donation
