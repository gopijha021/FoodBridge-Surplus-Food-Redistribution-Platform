import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, is_member, member_org_ids, require_member, require_roles
from app.core.errors import AppError
from app.db.models import Organisation, OrganisationMembership, User
from app.db.outbox import emit
from app.db.session import get_db
from app.domain.enums import EntityType
from app.domain.state import record_history
from app.modules.organisations.schemas import OrgCreateIn, OrgOut, VerificationIn

router = APIRouter(prefix="/organisations", tags=["organisations"])


def org_out(org: Organisation, exact: bool) -> OrgOut:
    return OrgOut(
        id=str(org.id), name=org.name, type=org.type, verification_status=org.verification_status,
        service_radius_km=org.service_radius_km, is_active=org.is_active,
        latitude=org.latitude if exact else None, longitude=org.longitude if exact else None,
        capacity=org.capacity if exact else None, accepted_categories=org.accepted_categories if exact else None,
        cold_storage=org.cold_storage if exact else None,
    )


@router.post("", response_model=OrgOut, status_code=201)
def create_organisation(body: OrgCreateIn, db: Session = Depends(get_db), user: User = Depends(require_roles("DONOR", "NGO"))):
    if body.type != user.role:
        raise AppError("WRONG_ORGANISATION_TYPE", f"{user.role} users can only create {user.role} organisations.", 422)
    org = Organisation(
        name=body.name, type=body.type, latitude=body.latitude, longitude=body.longitude,
        service_radius_km=body.service_radius_km, capacity=body.capacity,
        accepted_categories=body.accepted_categories, cold_storage=body.cold_storage,
    )
    db.add(org)
    db.flush()
    db.add(OrganisationMembership(user_id=user.id, organisation_id=org.id))
    record_history(db, EntityType.ORGANISATION, org.id, None, "UNVERIFIED", user.id, "created")
    db.commit()
    return org_out(org, True)


@router.get("", response_model=list[OrgOut])
def my_organisations(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ids = member_org_ids(db, user.id)
    orgs = db.scalars(select(Organisation).where(Organisation.id.in_(ids))).all() if ids else []
    return [org_out(o, True) for o in orgs]


@router.get("/{org_id}", response_model=OrgOut)
def get_organisation(org_id: uuid.UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    org = db.get(Organisation, org_id)
    if not org:
        raise AppError("ORGANISATION_NOT_FOUND", "Organisation not found.", 404)
    return org_out(org, user.role == "ADMIN" or is_member(db, user, org_id))


@router.post("/{org_id}/verification", response_model=OrgOut)
def submit_verification(org_id: uuid.UUID, body: VerificationIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    org = require_member(db, user, org_id)
    if org.verification_status == "VERIFIED":
        raise AppError("ALREADY_VERIFIED", "Organisation is already verified.", 409)
    prev = org.verification_status
    org.verification_evidence = body.evidence
    org.verification_status = "PENDING"
    record_history(db, EntityType.ORGANISATION, org.id, prev, "PENDING", user.id, "verification submitted")
    emit(db, "organisation", org.id, "VerificationSubmitted", {"org_id": str(org.id)})
    db.commit()
    return org_out(org, True)
