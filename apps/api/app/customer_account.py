"""Self-service profile and password-reauthenticated login-email ownership changes."""
# ruff: noqa: B008

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.auth import (
    get_current_user,
    hash_password,
    normalize_email,
    validate_password,
    verify_password,
)
from apps.api.app.database import get_db
from apps.api.app.email_outbox import enqueue
from apps.api.app.models import (
    EmailChangeToken,
    OAuthIdentity,
    PasswordResetToken,
    SamlIdentity,
    ScimUserMapping,
    User,
    UserSession,
)
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.security_notifications import audit_account, notify
from apps.api.app.settings import settings

router = APIRouter(prefix="/auth/account", tags=["customer account"])
INVALID = "This email-change link is invalid or has expired."


def managed(db: Session, user: User) -> bool:
    return any(
        db.scalar(select(model.id).where(model.user_id == user.id).limit(1)) is not None
        for model in (OAuthIdentity, SamlIdentity, ScimUserMapping)
    )


def reauthenticate(db: Session, user: User, password: str) -> None:
    if managed(db, user):
        raise HTTPException(
            403, "This login identity is managed by your identity provider or workspace."
        )
    if user.force_password_change or not verify_password(password, user.password_hash):
        raise HTTPException(403, "Current-password verification is required.")


def invalidate(db: Session, user: User, now: datetime) -> None:
    user.auth_version = (user.auth_version or 0) + 1
    db.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    for model in (EmailChangeToken, PasswordResetToken):
        db.execute(
            update(model)
            .where(model.user_id == user.id, model.used_at.is_(None))
            .values(used_at=now)
        )


class ProfileUpdate(BaseModel):
    display_name: str = Field(max_length=120)


class EmailRequest(BaseModel):
    new_email: EmailStr
    current_password: str = Field(min_length=1, max_length=128)


class ConfirmRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


@router.get("/profile")
def profile(user: User = Depends(get_current_user), db: Session = Depends(get_db)):  # noqa: B008
    return {
        "display_name": user.display_name or "",
        "email": user.email,
        "email_verified": user.email_verified,
        "identity_managed": managed(db, user),
    }


@router.patch("/profile")
def update_profile(
    payload: ProfileUpdate,
    user: User = Depends(get_current_user),  # noqa: B008
    db: Session = Depends(get_db),
):  # noqa: B008
    name = payload.display_name.strip()
    if any(ord(char) < 32 for char in name):
        raise HTTPException(422, "Display name contains invalid characters.")
    if (user.display_name or "") != name:
        user.display_name = name or None
        audit_account(db, user, "profile_updated")
        db.commit()
    return profile(user, db)


@router.post("/email-change")
def request_email(
    payload: EmailRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),
):  # noqa: B008
    limiter.check("email-change-peer", client_identifier(request), 5, 3600, fail_closed=True)
    limiter.check("email-change-user", str(user.id), 5, 3600, fail_closed=True)
    user = db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
    reauthenticate(db, user, payload.current_password)
    address = normalize_email(str(payload.new_email))
    if db.scalar(select(User.id).where(func.lower(User.email) == address)) is not None:
        raise HTTPException(409, "That login email is unavailable.")
    now = datetime.now(UTC)
    latest = db.scalar(
        select(EmailChangeToken.created_at)
        .where(EmailChangeToken.user_id == user.id)
        .order_by(EmailChangeToken.created_at.desc())
        .limit(1)
    )
    if latest and latest.replace(tzinfo=UTC) > now - timedelta(seconds=60):
        raise HTTPException(429, "Please wait before requesting another email change.")
    db.execute(
        update(EmailChangeToken)
        .where(EmailChangeToken.user_id == user.id, EmailChangeToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(48)
    record = EmailChangeToken(
        user_id=user.id,
        old_email=user.email,
        new_email=address,
        auth_version=user.auth_version or 0,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=60),
    )
    db.add(record)
    db.flush()
    mail = EmailMessage()
    mail["From"] = settings.smtp_from_address or "no-reply@localhost"
    mail["To"] = address
    mail["Subject"] = "Verify your new Invoxa login email"
    link = (
        settings.frontend_base_url.rstrip("/")
        + "/confirm-email-change?"
        + urlencode({"token": raw})
    )
    mail.set_content(
        f"Verify your new login email: {link}\nThis link expires in 60 minutes. "
        "Your current login address remains unchanged until verification."
    )
    enqueue(db, mail, "email-change", user_id=user.id, related_token_id=record.id)
    notify(db, user, "email_change_requested", event_id=record.id)
    audit_account(db, user, "email_change_requested")
    db.commit()
    return {"detail": "Verification sent to the new address. Your login email is unchanged."}


@router.post("/email-change/confirm")
def confirm_email(
    payload: ConfirmRequest,
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),
):  # noqa: B008
    limiter.check("email-change-confirm", client_identifier(request), 20, 300, fail_closed=True)
    token = db.scalar(
        select(EmailChangeToken).where(
            EmailChangeToken.token_hash == hashlib.sha256(payload.token.encode()).hexdigest()
        )
    )
    if token is None:
        raise HTTPException(400, INVALID)
    user = db.scalar(select(User).where(User.id == token.user_id).with_for_update())
    db.refresh(token)
    now = datetime.now(UTC)
    if (
        not user
        or not user.is_active
        or token.used_at is not None
        or token.expires_at.replace(tzinfo=UTC) <= now
        or token.old_email != user.email
        or token.auth_version != user.auth_version
        or managed(db, user)
    ):
        raise HTTPException(400, INVALID)
    if db.scalar(select(User.id).where(func.lower(User.email) == token.new_email)) is not None:
        raise HTTPException(409, "That login email is unavailable.")
    old = user.email
    user.email = token.new_email
    user.email_verified = True
    invalidate(db, user, now)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "That login email is unavailable.") from None
    audit_account(db, user, "login_email_changed")
    notify(db, user, "email_changed", recipient=old, event_id=token.id)
    notify(db, user, "email_changed", event_id=token.id)
    db.commit()
    return {
        "detail": "Login email changed. All sessions were revoked. Sign in with your new email."
    }


@router.post("/password")
def change_password(
    payload: PasswordRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),
):  # noqa: B008
    limiter.check("account-password", str(user.id), 5, 3600, fail_closed=True)
    user = db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
    reauthenticate(db, user, payload.current_password)
    user.password_hash = hash_password(validate_password(payload.new_password))
    invalidate(db, user, datetime.now(UTC))
    audit_account(db, user, "password_changed")
    notify(db, user, "password_changed")
    db.commit()
    return {"detail": "Password changed. Please sign in again."}
