"""Mandatory recipient-only notices; enqueue failure never rolls back a security action."""

import hashlib
import logging
import uuid
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.email_outbox import enqueue
from apps.api.app.models import OrganizationMember, User
from apps.api.app.security_audit import add_security_event
from apps.api.app.settings import settings

logger = logging.getLogger(__name__)
TEXT = {
    "account_deactivated": (
        "Your account was voluntarily deactivated. Existing sessions were revoked. "
        "Workspace documents remain preserved."
    ),
    "account_reactivated": "Your account was reactivated. Sign in again to create a new session.",
    "password_changed": "Your password was changed. Existing sessions were invalidated.",
    "password_reset": "Your password was reset. Existing sessions were invalidated.",
    "email_change_requested": (
        "A login-email change was requested. Your current login email remains unchanged "
        "until verification."
    ),
    "email_changed": (
        "Your login email was changed after ownership verification. "
        "Existing sessions were invalidated."
    ),
    "sessions_revoked": "One or more active sessions were revoked.",
}


def audit_account(db: Session, user: User, action: str) -> None:
    for org_id in db.scalars(
        select(OrganizationMember.organization_id).where(OrganizationMember.user_id == user.id)
    ).unique():
        add_security_event(
            db,
            organization_id=org_id,
            actor_user_id=user.id,
            subject_user_id=user.id,
            action=action,
            resource_type="user",
            resource_id=user.id,
        )


def notify(
    db: Session,
    user: User,
    action: str,
    *,
    recipient: str | None = None,
    event_id: uuid.UUID | None = None,
) -> bool:
    """Caller supplies a stable event ID for duplicate-safe replay of the same event."""
    try:
        with db.begin_nested():
            message = EmailMessage()
            message["From"] = settings.smtp_from_address or "no-reply@localhost"
            message["To"] = recipient or user.email
            message["Subject"] = "Invoxa account security notice"
            recipient_id = hashlib.sha256(str(message["To"]).encode()).hexdigest()[:16]
            message["Message-ID"] = (
                f"<security-{event_id or uuid.uuid4()}-{action}-{recipient_id}@invoxa.invalid>"
            )
            message.set_content(
                TEXT[action] + "\n\nIf this was not you, recover your account immediately "
                "using the password recovery page on your trusted Invoxa website.\n"
                "This is a mandatory account-security notice."
            )
            enqueue(db, message, "security-notice", user_id=user.id)
        return True
    except Exception:
        logger.warning("security_notice_enqueue_unavailable")
        return False
