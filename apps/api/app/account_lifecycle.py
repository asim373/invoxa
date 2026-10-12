"""Voluntary deactivation; permanent deletion awaits an approved retention policy."""
# ruff: noqa: B008

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.app.auth import (
    DUMMY_PASSWORD_HASH,
    get_current_user,
    hash_password,
    normalize_email,
    verify_password,
)
from apps.api.app.customer_account import invalidate, managed, reauthenticate
from apps.api.app.database import get_db
from apps.api.app.email_outbox import enqueue
from apps.api.app.models import (
    AccountDeletionTombstone,
    AccountReactivationToken,
    Document,
    EmailVerificationToken,
    OAuthLoginCode,
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.security_notifications import audit_account, notify
from apps.api.app.settings import settings

router = APIRouter(prefix="/auth/account", tags=["account lifecycle"])
GENERIC = {"detail": "If this account is eligible, recovery instructions will be delivered."}


class DeactivateRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    confirmation: str


class RecoveryRequest(BaseModel):
    email: EmailStr
    current_password: str = Field(min_length=1, max_length=128)


class ReactivateRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    current_password: str = Field(min_length=1, max_length=128)


class DeleteRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    confirmation: str


def protect_ownership(db: Session, user: User) -> None:
    org_ids = list(
        db.scalars(
            select(OrganizationMember.organization_id).where(
                OrganizationMember.user_id == user.id,
                OrganizationMember.role == OrganizationMemberRole.OWNER,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            )
        )
    )
    # Serialize simultaneous owner deactivations in a deterministic lock order.
    list(
        db.scalars(
            select(Organization)
            .where(Organization.id.in_(org_ids))
            .order_by(Organization.id)
            .with_for_update()
        )
    )
    for org_id in org_ids:
        other_owner = db.scalar(
            select(OrganizationMember.id)
            .join(User)
            .where(
                OrganizationMember.organization_id == org_id,
                OrganizationMember.user_id != user.id,
                OrganizationMember.role == OrganizationMemberRole.OWNER,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
                User.is_active.is_(True),
            )
            .limit(1)
        )
        if other_owner is None:
            raise HTTPException(
                409, "Another active OWNER is required before account deactivation."
            )


@router.post("/deactivate")
def deactivate(
    payload: DeactivateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    limiter.check("account-deactivate", str(user.id), 5, 3600, fail_closed=True)
    if payload.confirmation != "DEACTIVATE":
        raise HTTPException(422, "Type DEACTIVATE to confirm.")
    user = db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
    reauthenticate(db, user, payload.current_password)
    protect_ownership(db, user)
    now = datetime.now(UTC)
    user.is_active = False
    user.self_deactivated_at = now
    invalidate(db, user, now)
    db.execute(
        update(AccountReactivationToken)
        .where(
            AccountReactivationToken.user_id == user.id, AccountReactivationToken.used_at.is_(None)
        )
        .values(used_at=now)
    )
    audit_account(db, user, "account_deactivated")
    notify(db, user, "account_deactivated")
    db.commit()
    return {"detail": "Account deactivated. Workspace documents and audit records are preserved."}


@router.post("/reactivation-request")
def recovery(
    payload: RecoveryRequest,
    request: Request,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    address = normalize_email(str(payload.email))
    limiter.check("account-recovery-peer", client_identifier(request), 5, 3600, fail_closed=True)
    limiter.check("account-recovery-address", address, 5, 3600, fail_closed=True)
    user = db.scalar(select(User).where(User.email == address).with_for_update())
    password_valid = verify_password(
        payload.current_password, user.password_hash if user else DUMMY_PASSWORD_HASH
    )
    if (
        not user
        or user.is_active
        or user.self_deactivated_at is None
        or managed(db, user)
        or not password_valid
    ):
        return GENERIC
    now = datetime.now(UTC)
    latest = db.scalar(
        select(AccountReactivationToken.created_at)
        .where(AccountReactivationToken.user_id == user.id)
        .order_by(AccountReactivationToken.created_at.desc())
        .limit(1)
    )
    if latest and latest.replace(tzinfo=UTC) > now - timedelta(seconds=60):
        return GENERIC
    db.execute(
        update(AccountReactivationToken)
        .where(
            AccountReactivationToken.user_id == user.id, AccountReactivationToken.used_at.is_(None)
        )
        .values(used_at=now)
    )
    raw = secrets.token_urlsafe(48)
    record = AccountReactivationToken(
        user_id=user.id,
        auth_version=user.auth_version,
        token_hash=hashlib.sha256(raw.encode()).hexdigest(),
        expires_at=now + timedelta(minutes=60),
    )
    db.add(record)
    db.flush()
    mail = EmailMessage()
    mail["From"] = settings.smtp_from_address or "no-reply@localhost"
    mail["To"] = address
    mail["Subject"] = "Reactivate your Invoxa account"
    link = (
        settings.frontend_base_url.rstrip("/") + "/reactivate-account?" + urlencode({"token": raw})
    )
    mail.set_content(
        f"Confirm account reactivation: {link}\nThis link expires in 60 minutes. "
        "Your current password is also required. No session is granted by this link."
    )
    enqueue(db, mail, "account-reactivation", user_id=user.id, related_token_id=record.id)
    audit_account(db, user, "account_reactivation_requested")
    db.commit()
    return GENERIC


@router.post("/reactivate")
def reactivate(
    payload: ReactivateRequest,
    request: Request,
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    limiter.check("account-reactivate", client_identifier(request), 10, 300, fail_closed=True)
    token = db.scalar(
        select(AccountReactivationToken).where(
            AccountReactivationToken.token_hash
            == hashlib.sha256(payload.token.encode()).hexdigest()
        )
    )
    if token is None:
        raise HTTPException(400, "Recovery link is invalid or has expired.")
    user = db.scalar(select(User).where(User.id == token.user_id).with_for_update())
    db.refresh(token)
    now = datetime.now(UTC)
    if (
        not user
        or user.is_active
        or user.self_deactivated_at is None
        or managed(db, user)
        or token.used_at is not None
        or token.expires_at.replace(tzinfo=UTC) <= now
        or user.auth_version != token.auth_version
        or not verify_password(payload.current_password, user.password_hash)
    ):
        raise HTTPException(400, "Recovery link is invalid or has expired.")
    user.is_active = True
    user.self_deactivated_at = None
    invalidate(db, user, now)
    db.execute(
        update(AccountReactivationToken)
        .where(
            AccountReactivationToken.user_id == user.id, AccountReactivationToken.used_at.is_(None)
        )
        .values(used_at=now)
    )
    audit_account(db, user, "account_reactivated")
    notify(db, user, "account_reactivated", event_id=token.id)
    db.commit()
    return {"detail": "Account reactivated. Sign in again; old sessions remain revoked."}


def reconcile_deleted_accounts(db: Session) -> int:
    """Idempotently enforce deletion tombstones after a supported database restore.

    Operators retain this minimal table with backup recovery material; replaying it never
    reactivates a deleted identity and never touches organization-owned documents.
    """
    reconciled = 0
    for marker in db.scalars(select(AccountDeletionTombstone)).all():
        user = db.get(User, marker.user_id)
        if user is not None and user.deleted_at is None:
            _anonymize_deleted_user(db, user, marker.completed_at)
            reconciled += 1
    return reconciled


def _personal_document_paths(db: Session, user: User) -> list[Path]:
    """Remove only standalone files owned exclusively by this account."""
    root = settings.storage_path.resolve()
    files: list[Path] = []
    for document in db.scalars(
        select(Document).where(Document.owner_id == user.id, Document.organization_id.is_(None))
    ):
        candidate = (root / document.storage_location).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as error:
            raise HTTPException(500, "Personal document storage location is invalid.") from error
        files.append(candidate)
        db.delete(document)
    return files


def _anonymize_deleted_user(db: Session, user: User, now: datetime) -> list[Path]:
    """Remove direct account identity while preserving legally necessary shared references."""
    former_email = user.email
    personal_files = _personal_document_paths(db, user)
    db.execute(
        update(OrganizationMember)
        .where(OrganizationMember.user_id == user.id)
        .values(status=OrganizationMemberStatus.INACTIVE, deactivated_at=now)
    )
    db.execute(
        update(OrganizationInvitation)
        .where(
            OrganizationInvitation.invited_by_user_id == user.id,
            OrganizationInvitation.status == OrganizationInvitationStatus.PENDING,
        )
        .values(status=OrganizationInvitationStatus.REVOKED, revoked_at=now)
    )
    db.execute(
        update(OrganizationInvitation)
        .where(
            OrganizationInvitation.invited_email == former_email,
            OrganizationInvitation.status == OrganizationInvitationStatus.PENDING,
        )
        .values(status=OrganizationInvitationStatus.REVOKED, revoked_at=now)
    )
    for token_model in (AccountReactivationToken, EmailVerificationToken, OAuthLoginCode):
        db.execute(
            update(token_model)
            .where(token_model.user_id == user.id, token_model.used_at.is_(None))
            .values(used_at=now)
        )
    # Organization documents retain their business history and organization binding.
    # Their user references point to a non-identifying tombstone. Standalone personal
    # files are deleted after the database transaction commits.
    user.email = f"deleted-{user.id}@deleted.invoxa.invalid"
    user.display_name = None
    user.email_verified = False
    user.password_hash = hash_password(secrets.token_urlsafe(32))
    user.force_password_change = True
    user.is_active = False
    user.self_deactivated_at = None
    user.deleted_at = now
    invalidate(db, user, now)
    return personal_files


@router.post("/delete")
def delete_account(
    payload: DeleteRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
):
    """Irreversibly anonymize an eligible password account; no shared data is deleted."""
    limiter.check("account-delete", str(user.id), 3, 3600, fail_closed=True)
    if payload.confirmation != "DELETE":
        raise HTTPException(422, "Type DELETE to confirm permanent account deletion.")
    user = db.execute(select(User).where(User.id == user.id).with_for_update()).scalar_one()
    reauthenticate(db, user, payload.current_password)
    protect_ownership(db, user)
    if user.deleted_at is not None:
        raise HTTPException(409, "This account has already been deleted.")
    now = datetime.now(UTC)
    marker = AccountDeletionTombstone(user_id=user.id, completed_at=now)
    db.add(marker)
    personal_files = _anonymize_deleted_user(db, user, now)
    audit_account(db, user, "account_deleted")
    quarantined: list[tuple[Path, Path]] = []
    try:
        for file_path in personal_files:
            if file_path.exists():
                quarantine = file_path.with_name(
                    f".{file_path.name}.{secrets.token_hex(8)}.deleting"
                )
                file_path.replace(quarantine)
                quarantined.append((file_path, quarantine))
        db.commit()
    except Exception:
        db.rollback()
        for original, quarantine in quarantined:
            try:
                quarantine.replace(original)
            except OSError:
                pass
        raise HTTPException(500, "Account deletion could not be completed.") from None
    for _, quarantine in quarantined:
        try:
            quarantine.unlink()
        except OSError:
            # Quarantined data is no longer reachable by the deleted account. Storage
            # maintenance may safely retry removal without restoring the identity.
            pass
    return {
        "detail": "Account permanently deleted. Shared workspace records were preserved.",
    }
