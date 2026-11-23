import uuid

from fastapi import APIRouter, Depends, Header, Query, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, member_org_ids, require_member, require_roles
from app.core.errors import AppError
from app.core.ratelimit import RateLimiter
from app.db.models import Allocation, DeliveryTask, Donation, Organisation, StatusHistory, User
from app.db.session import get_db
from app.modules.allocations.service import claim_donation
from app.modules.donations import service
from app.modules.donations.schemas import AllocationOut, ClaimIn, DonationCreateIn, DonationOut, ReasonIn
from app.modules.matching.service import nearby_donations_for_ngo, rank_candidates

router = APIRouter(prefix="/donations", tags=["donations"])
claim_limit = RateLimiter("claim", 30, 60)


def _donor_member(db: Session, user: User, d: Donation) -> None:
    require_member(db, user, d.donor_org_id)


@router.post("", response_model=DonationOut, status_code=201)
def create_donation(body: DonationCreateIn, db: Session = Depends(get_db), user: User = Depends(require_roles("DONOR"))):
    d = service.create_draft(db, user, body)
    return service.present(db, d, True)


@router.get("")
def list_donations(
    status: str | None = None, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
    db: Session = Depends(get_db), user: User = Depends(require_roles("DONOR", "ADMIN")),
):
    stmt = select(Donation)
    if user.role != "ADMIN":
        stmt = stmt.where(Donation.donor_org_id.in_(member_org_ids(db, user.id) or {uuid.uuid4()}))
    if status:
        stmt = stmt.where(Donation.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(Donation.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [service.present(db, d, True) for d in rows], "total": total, "limit": limit, "offset": offset}


@router.get("/nearby")
def nearby(
    ngo_org_id: uuid.UUID, latitude: float | None = Query(None, ge=-90, le=90),
    longitude: float | None = Query(None, ge=-180, le=180), radius_km: float | None = Query(None, gt=0, le=200),
    db: Session = Depends(get_db), user: User = Depends(require_roles("NGO", "ADMIN")),
):
    """Eligible, ranked donations for an NGO, with score components for explainability."""
    ngo: Organisation = require_member(db, user, ngo_org_id, "NGO")
    lat = latitude if latitude is not None else ngo.latitude
    lon = longitude if longitude is not None else ngo.longitude
    radius = min(radius_km or ngo.service_radius_km, ngo.service_radius_km)
    items = nearby_donations_for_ngo(db, ngo, lat, lon, radius)
    return {"items": [
        {"donation": service.present(db, i["donation"], False), "distance_km": i["distance_km"],
         "match": {k: i[k] for k in ("score", "proximity_score", "capacity_fit", "pickup_readiness", "historical_fulfilment")}}
        for i in items]}


@router.get("/{donation_id}", response_model=DonationOut)
def get_donation(donation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = service.get_donation_or_404(db, donation_id)
    exact = service.can_see_exact(db, user, d)
    if not exact:
        # outsiders only see live listings, and only NGOs (volunteers use /tasks)
        if not (user.role == "NGO" and d.status == "PUBLISHED"):
            raise AppError("DONATION_NOT_FOUND", "Donation not found.", 404)
    return service.present(db, d, exact)


@router.get("/{donation_id}/candidates")
def candidates(donation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_roles("DONOR", "ADMIN"))):
    d = service.get_donation_or_404(db, donation_id)
    _donor_member(db, user, d)
    return {"items": [{**c, "ngo_org_id": str(c["ngo_org_id"])} for c in rank_candidates(db, d)]}


@router.get("/{donation_id}/history")
def history(donation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    d = service.get_donation_or_404(db, donation_id)
    if not service.can_see_exact(db, user, d):
        raise AppError("DONATION_NOT_FOUND", "Donation not found.", 404)
    alloc_ids = list(db.scalars(select(Allocation.id).where(Allocation.donation_id == d.id)))
    task_ids = list(db.scalars(select(DeliveryTask.id).where(DeliveryTask.allocation_id.in_(alloc_ids)))) if alloc_ids else []
    ids = [d.id, *alloc_ids, *task_ids]
    rows = db.scalars(select(StatusHistory).where(StatusHistory.entity_id.in_(ids)).order_by(StatusHistory.created_at)).all()
    return {"items": [{"entity_type": r.entity_type, "entity_id": str(r.entity_id), "from": r.from_status, "to": r.to_status,
                       "actor_id": str(r.actor_id) if r.actor_id else None, "reason": r.reason,
                       "at": r.created_at.isoformat()} for r in rows]}


@router.post("/{donation_id}/publish", response_model=DonationOut)
def publish(donation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(require_roles("DONOR"))):
    d = service.get_donation_or_404(db, donation_id, lock=True)
    _donor_member(db, user, d)
    return service.present(db, service.publish(db, d, user), True)


@router.post("/{donation_id}/withdraw", response_model=DonationOut)
def withdraw(donation_id: uuid.UUID, body: ReasonIn | None = None, db: Session = Depends(get_db),
             user: User = Depends(require_roles("DONOR"))):
    d = service.get_donation_or_404(db, donation_id, lock=True)
    _donor_member(db, user, d)
    return service.present(db, service.withdraw(db, d, user, body.reason if body else None), True)


@router.post("/{donation_id}/cancel", response_model=DonationOut)
def cancel(donation_id: uuid.UUID, body: ReasonIn | None = None, db: Session = Depends(get_db),
           user: User = Depends(require_roles("DONOR", "ADMIN"))):
    d = service.get_donation_or_404(db, donation_id, lock=True)
    _donor_member(db, user, d)
    return service.present(db, service.cancel(db, d, user, body.reason if body else None), True)


@router.post("/{donation_id}/claim", response_model=AllocationOut, status_code=201, dependencies=[Depends(claim_limit)])
def claim(
    donation_id: uuid.UUID, body: ClaimIn, response: Response,
    idempotency_key: str = Header(..., alias="Idempotency-Key", min_length=8, max_length=128),
    db: Session = Depends(get_db), user: User = Depends(require_roles("NGO")),
):
    alloc, created = claim_donation(db, user=user, donation_id=donation_id, ngo_org_id=body.ngo_org_id,
                                    idempotency_key=idempotency_key)
    if not created:  # idempotent replay: return the original result
        response.status_code = 200
    task = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == alloc.id))
    d = db.get(Donation, alloc.donation_id)
    return AllocationOut(id=str(alloc.id), donation_id=str(alloc.donation_id), ngo_org_id=str(alloc.ngo_org_id),
                         allocated_quantity=alloc.allocated_quantity, status=alloc.status, donation_status=d.status,
                         task_id=str(task.id) if task else None)
