import uuid

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, is_member, require_roles
from app.core.errors import AppError
from app.db.models import Allocation, DeliveryTask, Donation, User
from app.db.session import get_db
from app.modules.allocations import service
from app.modules.delivery.service import assign_volunteer

router = APIRouter(prefix="/allocations", tags=["allocations"])


class AssignIn(BaseModel):
    volunteer_id: uuid.UUID | None = None  # omit to auto-select the nearest available volunteer


class ReceiptIn(BaseModel):
    received_quantity: float = Field(ge=0)
    condition_ok: bool = True
    notes: str | None = Field(default=None, max_length=2000)


class ReleaseIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


def _out(db: Session, a: Allocation) -> dict:
    d = db.get(Donation, a.donation_id)
    t = db.scalar(select(DeliveryTask).where(DeliveryTask.allocation_id == a.id))
    return {"id": str(a.id), "donation_id": str(a.donation_id), "ngo_org_id": str(a.ngo_org_id),
            "allocated_quantity": a.allocated_quantity, "status": a.status, "donation_status": d.status,
            "task_id": str(t.id) if t else None, "task_status": t.status if t else None,
            "volunteer_id": str(t.volunteer_id) if t and t.volunteer_id else None}


@router.get("/{allocation_id}")
def get_allocation(allocation_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    a = service.get_allocation_or_404(db, allocation_id)
    d = db.get(Donation, a.donation_id)
    if not (user.role == "ADMIN" or is_member(db, user, a.ngo_org_id) or is_member(db, user, d.donor_org_id)):
        raise AppError("ALLOCATION_NOT_FOUND", "Allocation not found.", 404)
    return _out(db, a)


@router.post("/{allocation_id}/assign")
def assign(allocation_id: uuid.UUID, body: AssignIn | None = None, db: Session = Depends(get_db),
           user: User = Depends(require_roles("NGO"))):
    assign_volunteer(db, user, allocation_id, body.volunteer_id if body else None)
    return _out(db, service.get_allocation_or_404(db, allocation_id))


@router.post("/{allocation_id}/release")
def release(allocation_id: uuid.UUID, body: ReleaseIn | None = None, db: Session = Depends(get_db),
            user: User = Depends(require_roles("NGO"))):
    return _out(db, service.release_allocation(db, user, allocation_id, body.reason if body else None))


@router.post("/{allocation_id}/confirm-receipt")
def confirm_receipt(allocation_id: uuid.UUID, body: ReceiptIn, db: Session = Depends(get_db),
                    user: User = Depends(require_roles("NGO"))):
    alloc, _ = service.confirm_receipt(db, user, allocation_id, body.received_quantity, body.condition_ok, body.notes)
    return _out(db, alloc)
