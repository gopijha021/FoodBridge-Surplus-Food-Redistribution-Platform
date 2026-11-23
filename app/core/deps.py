import uuid

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import decode_access_token
from app.db.models import Organisation, OrganisationMembership, User
from app.db.session import get_db

bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer), db: Session = Depends(get_db)
) -> User:
    if creds is None:
        raise AppError("NOT_AUTHENTICATED", "Authentication required.", 401)
    data = decode_access_token(creds.credentials)
    user = db.get(User, uuid.UUID(data["sub"]))
    if not user or not user.is_active:
        raise AppError("INVALID_TOKEN", "User not found or inactive.", 401)
    return user


def require_roles(*roles: str):
    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role not in {str(r) for r in roles}:
            raise AppError("FORBIDDEN_ROLE", "Your role cannot perform this action.", 403)
        return user

    return dep


def member_org_ids(db: Session, user_id: uuid.UUID) -> set[uuid.UUID]:
    return set(db.scalars(select(OrganisationMembership.organisation_id).where(OrganisationMembership.user_id == user_id)))


def is_member(db: Session, user: User, org_id: uuid.UUID) -> bool:
    return org_id in member_org_ids(db, user.id)


def require_member(db: Session, user: User, org_id: uuid.UUID, org_type: str | None = None) -> Organisation:
    """Every protected endpoint enforces organisation membership here (admins bypass)."""
    org = db.get(Organisation, org_id)
    if not org:
        raise AppError("ORGANISATION_NOT_FOUND", "Organisation not found.", 404)
    if org_type and org.type != str(org_type):
        raise AppError("WRONG_ORGANISATION_TYPE", f"Organisation must be of type {org_type}.", 422)
    if user.role != "ADMIN" and not is_member(db, user, org_id):
        raise AppError("NOT_ORGANISATION_MEMBER", "You are not a member of this organisation.", 403)
    return org
