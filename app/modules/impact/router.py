import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_member
from app.db.models import ImpactRecord, User
from app.db.session import get_db

router = APIRouter(prefix="/organisations", tags=["impact"])


@router.get("/{org_id}/impact")
def org_impact(org_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = require_member(db, user, org_id)
    col = ImpactRecord.donor_org_id if org.type == "DONOR" else ImpactRecord.ngo_org_id
    rows = db.execute(select(ImpactRecord.unit, func.sum(ImpactRecord.quantity_redistributed), func.count())
                      .where(col == org_id).group_by(ImpactRecord.unit)).all()
    return {"organisation_id": str(org_id), "role": org.type, "completed_donations": sum(r[2] for r in rows),
            "by_unit": [{"unit": u, "quantity_redistributed": float(q or 0), "donations": n} for u, q, n in rows]}
