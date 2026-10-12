"""Email ownership verification; bearer secrets never enter audit records."""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.app.database import get_db
from apps.api.app.email import VerificationEmailSender, get_verification_email_sender
from apps.api.app.models import EmailVerificationToken, OrganizationMember, User
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.security_audit import add_security_event
from apps.api.app.settings import settings

router = APIRouter(prefix="/auth", tags=["email verification"])
GENERIC_RESPONSE = {"detail": "If verification is needed, a verification email will be sent."}


def audit_verification(db: Session, user: User, action: str) -> None:
    for membership in db.scalars(
        select(OrganizationMember).where(OrganizationMember.user_id == user.id)
    ):
        add_security_event(
            db,
            organization_id=membership.organization_id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            action=action,
            resource_type="user",
            resource_id=user.id,
        )


def issue_verification(db: Session, user: User, sender: VerificationEmailSender) -> None:
    now = datetime.now(UTC)
    db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user.id, EmailVerificationToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(48)
    db.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=now + timedelta(minutes=60),
            created_at=now,
        )
    )
    sender.send_verification(
        user.email,
        f"{settings.frontend_base_url.rstrip('/')}/verify-email?{urlencode({'token': raw})}",
    )
    audit_verification(db, user, "email_verification_requested")


class VerifyRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class ResendRequest(BaseModel):
    email: EmailStr


@router.post("/verify-email")
def verify_email(
    payload: VerifyRequest,
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
):
    limiter.check("email-verification", client_identifier(request), 20, 300, fail_closed=True)
    digest = hashlib.sha256(payload.token.encode()).hexdigest()
    # Compare-and-set token consumption also prevents concurrent double use.
    token = db.scalar(
        select(EmailVerificationToken).where(EmailVerificationToken.token_hash == digest)
    )
    now = datetime.now(UTC)
    if token is None:
        raise HTTPException(400, "This verification link is invalid or has expired.")
    user = db.scalar(select(User).where(User.id == token.user_id).with_for_update())
    consumed = db.execute(
        update(EmailVerificationToken)
        .execution_options(synchronize_session=False)
        .where(
            EmailVerificationToken.id == token.id,
            EmailVerificationToken.used_at.is_(None),
            EmailVerificationToken.expires_at > now,
        )
        .values(used_at=now)
        .returning(EmailVerificationToken.id)
    ).scalar_one_or_none()
    if consumed is None or user is None or not user.is_active:
        db.rollback()
        raise HTTPException(400, "This verification link is invalid or has expired.")
    user.email_verified = True
    db.execute(
        update(EmailVerificationToken)
        .where(EmailVerificationToken.user_id == user.id, EmailVerificationToken.used_at.is_(None))
        .values(used_at=now)
    )
    audit_verification(db, user, "email_verified")
    db.commit()
    return {"detail": "Email verified. You can now sign in."}


@router.post("/resend-verification")
def resend_verification(
    payload: ResendRequest,
    request: Request,
    db: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    sender: VerificationEmailSender = Depends(get_verification_email_sender),  # noqa: B008
):
    address = str(payload.email).strip().casefold()
    limiter.check("verification-resend-peer", client_identifier(request), 5, 3600, fail_closed=True)
    limiter.check("verification-resend-address", address, 5, 3600, fail_closed=True)
    user = db.scalar(select(User).where(User.email == address).with_for_update())
    if user is None or not user.is_active or user.email_verified:
        return GENERIC_RESPONSE
    latest = db.scalar(
        select(EmailVerificationToken.created_at)
        .where(EmailVerificationToken.user_id == user.id)
        .order_by(EmailVerificationToken.created_at.desc())
        .limit(1)
    )
    if latest is not None:
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=UTC)
        if latest > datetime.now(UTC) - timedelta(seconds=60):
            return GENERIC_RESPONSE
    try:
        issue_verification(db, user, sender)
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(503, "Verification delivery is temporarily unavailable.") from None
    return GENERIC_RESPONSE
