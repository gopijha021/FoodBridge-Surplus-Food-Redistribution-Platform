import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.deps import require_roles
from app.core.errors import AppError
from app.db.models import Organisation, StatusHistory, User
from app.db.outbox import emit
from app.db.session import get_db
from app.domain.enums import EntityType
from app.domain.state import record_history
from app.modules.allocations.service import resolve_dispute

router = APIRouter(prefix="/admin", tags=["admin"])
Admin = Depends(require_roles("ADMIN"))


class VerifyIn(BaseModel):
    approve: bool
    reason: str | None = Field(default=None, max_length=1000)


class BlockIn(BaseModel):
    blocked: bool
    reason: str = Field(min_length=3, max_length=1000)


class ResolveIn(BaseModel):
    outcome: str = Field(pattern="^(COMPLETED|CANCELLED)$")
    quantity: float | None = Field(default=None, ge=0)
    reason: str = Field(min_length=3, max_length=2000)


@router.get("/audit")
def audit(entity_type: str | None = None, entity_id: uuid.UUID | None = None, actor_id: uuid.UUID | None = None,
          since: datetime | None = None, until: datetime | None = None, limit: int = Query(50, ge=1, le=200),
          offset: int = Query(0, ge=0), db: Session = Depends(get_db), _: User = Admin):
    stmt = select(StatusHistory)
    if entity_type:
        stmt = stmt.where(StatusHistory.entity_type == entity_type.upper())
    if entity_id:
        stmt = stmt.where(StatusHistory.entity_id == entity_id)
    if actor_id:
        stmt = stmt.where(StatusHistory.actor_id == actor_id)
    if since:
        stmt = stmt.where(StatusHistory.created_at >= since)
    if until:
        stmt = stmt.where(StatusHistory.created_at <= until)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = db.scalars(stmt.order_by(StatusHistory.created_at.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [
        {"id": str(r.id), "entity_type": r.entity_type, "entity_id": str(r.entity_id), "from": r.from_status,
         "to": r.to_status, "actor_id": str(r.actor_id) if r.actor_id else None, "reason": r.reason,
         "correlation_id": r.correlation_id, "at": r.created_at.isoformat()} for r in rows]}


@router.get("/organisations")
def list_orgs(verification_status: str | None = None, limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db), _: User = Admin):
    stmt = select(Organisation)
    if verification_status:
        stmt = stmt.where(Organisation.verification_status == verification_status.upper())
    rows = db.scalars(stmt.order_by(Organisation.created_at.desc()).limit(limit)).all()
    return {"items": [{"id": str(o.id), "name": o.name, "type": o.type, "verification_status": o.verification_status,
                       "evidence": o.verification_evidence, "is_blocked": o.is_blocked} for o in rows]}


@router.post("/organisations/{org_id}/verify")
def verify_org(org_id: uuid.UUID, body: VerifyIn, db: Session = Depends(get_db), admin: User = Admin):
    org = db.get(Organisation, org_id)
    if not org:
        raise AppError("ORGANISATION_NOT_FOUND", "Organisation not found.", 404)
    prev, new = org.verification_status, ("VERIFIED" if body.approve else "REJECTED")
    org.verification_status = new
    record_history(db, EntityType.ORGANISATION, org.id, prev, new, admin.id, body.reason)
    emit(db, "organisation", org.id, "OrganisationVerified" if body.approve else "OrganisationRejected", {"org_id": str(org.id)})
    db.commit()
    return {"id": str(org.id), "verification_status": new}


@router.post("/organisations/{org_id}/block")
def block_org(org_id: uuid.UUID, body: BlockIn, db: Session = Depends(get_db), admin: User = Admin):
    org = db.get(Organisation, org_id)
    if not org:
        raise AppError("ORGANISATION_NOT_FOUND", "Organisation not found.", 404)
    record_history(db, EntityType.ORGANISATION, org.id, "BLOCKED" if org.is_blocked else "ACTIVE",
                   "BLOCKED" if body.blocked else "ACTIVE", admin.id, body.reason)
    org.is_blocked = body.blocked
    db.commit()
    return {"id": str(org.id), "is_blocked": org.is_blocked}


@router.post("/donations/{donation_id}/resolve-dispute")
def resolve(donation_id: uuid.UUID, body: ResolveIn, db: Session = Depends(get_db), admin: User = Admin):
    d = resolve_dispute(db, admin, donation_id, body.outcome, body.quantity, body.reason)
    return {"id": str(d.id), "status": d.status}
