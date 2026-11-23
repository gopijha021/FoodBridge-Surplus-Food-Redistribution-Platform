from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.deps import get_current_user
from app.core.errors import AppError
from app.core.ratelimit import RateLimiter
from app.core.security import (
    create_access_token, hash_password, hash_refresh_token, new_refresh_token, verify_password,
)
from app.db.models import RefreshToken, User, utcnow
from app.db.session import get_db
from app.modules.auth.schemas import LoginIn, RefreshIn, RegisterIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])
login_limit = RateLimiter("login", 10, 60)
register_limit = RateLimiter("register", 10, 60)


def _user_out(u: User) -> UserOut:
    return UserOut(id=str(u.id), name=u.name, email=u.email, role=u.role)


def _issue(db: Session, user: User) -> TokenOut:
    raw, hashed = new_refresh_token()
    db.add(RefreshToken(user_id=user.id, token_hash=hashed, expires_at=utcnow() + timedelta(days=settings.refresh_token_ttl_days)))
    db.commit()
    return TokenOut(access_token=create_access_token(user.id, user.role), refresh_token=raw,
                    expires_in=settings.access_token_ttl_minutes * 60)


@router.post("/register", response_model=UserOut, status_code=201, dependencies=[Depends(register_limit)])
def register(body: RegisterIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise AppError("EMAIL_TAKEN", "An account with this email already exists.", 409)
    user = User(name=body.name, email=email, phone=body.phone, password_hash=hash_password(body.password), role=body.role)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError("EMAIL_TAKEN", "An account with this email already exists.", 409)
    return _user_out(user)


@router.post("/login", response_model=TokenOut, dependencies=[Depends(login_limit)])
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == body.email.lower()))
    # same error for unknown user / wrong password / inactive: no account enumeration
    if not user or not user.is_active or not verify_password(body.password, user.password_hash):
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password.", 401)
    return _issue(db, user)


@router.post("/refresh", response_model=TokenOut, dependencies=[Depends(login_limit)])
def refresh(body: RefreshIn, db: Session = Depends(get_db)):
    """Rotating refresh tokens: each token works once. Replaying a used token is rejected."""
    tok = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token)).with_for_update())
    if not tok or tok.revoked_at is not None or tok.expires_at <= utcnow():
        raise AppError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired.", 401)
    user = db.get(User, tok.user_id)
    if not user or not user.is_active:
        raise AppError("INVALID_REFRESH_TOKEN", "Refresh token is invalid or expired.", 401)
    tok.revoked_at = utcnow()
    return _issue(db, user)


@router.post("/logout", status_code=204)
def logout(body: RefreshIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    tok = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(body.refresh_token),
                                               RefreshToken.user_id == user.id))
    if tok and tok.revoked_at is None:
        tok.revoked_at = utcnow()
        db.commit()


@router.get("/me", response_model=UserOut)
def me(user: User = Depends(get_current_user)):
    return _user_out(user)
