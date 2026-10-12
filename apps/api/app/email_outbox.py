"""Database is authoritative; the existing Redis-backed worker polls this outbox.

Claims expire after 60 seconds, beyond SMTP's 10-second socket timeout.
SMTP acknowledgement and DB commit cannot be atomic: crash recovery is at-least-once.
"""

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from urllib.parse import parse_qs, urlparse

from cryptography.fernet import Fernet
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.models import (
    AccountReactivationToken,
    EmailChangeToken,
    EmailVerificationToken,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    PasswordResetToken,
    TransactionalEmail,
)
from apps.api.app.settings import settings

MAX_ATTEMPTS = 5


def cipher() -> Fernet:
    key = hashlib.sha256(
        b"invoxa-transactional-email-v1:" + settings.auth_secret_key.get_secret_value().encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def enqueue(
    db: Session,
    message: EmailMessage,
    kind: str,
    organization_id=None,
    user_id=None,
    related_token_id=None,
) -> TransactionalEmail:
    body = message.as_bytes()
    key = hashlib.sha256(kind.encode() + b":" + body).hexdigest()
    existing = db.scalar(
        select(TransactionalEmail).where(TransactionalEmail.idempotency_key == key)
    )
    if existing is not None:
        return existing
    record = TransactionalEmail(
        idempotency_key=key,
        kind=kind,
        encrypted_message=cipher().encrypt(body).decode(),
        next_attempt_at=datetime.now(UTC),
        status="pending",
        attempts=0,
        organization_id=organization_id,
        user_id=user_id,
        related_token_id=related_token_id,
    )
    try:
        with db.begin_nested():
            db.add(record)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(TransactionalEmail).where(TransactionalEmail.idempotency_key == key)
        )
        if existing is None:
            raise
        return existing
    return record


def enqueue_link(db: Session, message: EmailMessage, kind: str, link: str) -> TransactionalEmail:
    """Attach token ownership without persisting another plaintext token."""
    db.flush()
    raw = parse_qs(urlparse(link).query).get("token", [""])[0]
    model = {
        "invitation": OrganizationInvitation,
        "password-reset": PasswordResetToken,
        "email-verification": EmailVerificationToken,
    }[kind]
    token = db.scalar(
        select(model).where(model.token_hash == hashlib.sha256(raw.encode()).hexdigest())
    )
    if token is None:
        raise ValueError("Email token is not persisted in the transaction")
    return enqueue(
        db,
        message,
        kind,
        organization_id=getattr(token, "organization_id", None),
        user_id=getattr(token, "user_id", None),
        related_token_id=token.id,
    )


def token_for(db: Session, record: TransactionalEmail):
    model = {
        "invitation": OrganizationInvitation,
        "password-reset": PasswordResetToken,
        "email-verification": EmailVerificationToken,
        "email-change": EmailChangeToken,
        "account-reactivation": AccountReactivationToken,
    }.get(record.kind)
    return db.get(model, record.related_token_id) if model and record.related_token_id else None


def process_email(db: Session, deliver=None) -> bool:
    from apps.api.app.email import _deliver_or_outbox

    deliver = deliver or _deliver_or_outbox
    now = datetime.now(UTC)
    due = or_(
        and_(
            TransactionalEmail.status.in_(["pending", "retry"]),
            TransactionalEmail.next_attempt_at <= now,
        ),
        and_(TransactionalEmail.status == "sending", TransactionalEmail.lease_until <= now),
    )
    record = db.scalar(
        select(TransactionalEmail)
        .where(due)
        .order_by(TransactionalEmail.next_attempt_at, TransactionalEmail.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if record is None:
        db.rollback()
        return False
    claim = uuid.uuid4()
    claimed = db.execute(
        update(TransactionalEmail)
        .execution_options(synchronize_session=False)
        .where(TransactionalEmail.id == record.id, due)
        .values(
            status="sending",
            claim_id=claim,
            lease_until=now + timedelta(seconds=60),
            attempts=TransactionalEmail.attempts + 1,
        )
        .returning(TransactionalEmail.id)
    ).scalar()
    if claimed is None:
        db.rollback()
        return False
    db.commit()
    db.refresh(record)
    token = token_for(db, record)
    if record.related_token_id is not None:
        expiry = getattr(token, "expires_at", now)
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=UTC)
        invalid = (
            token is None
            or expiry <= now
            or getattr(token, "used_at", None) is not None
            or (
                isinstance(token, OrganizationInvitation)
                and token.status != OrganizationInvitationStatus.PENDING
            )
        )
        if invalid:
            record.status = "cancelled"
            record.encrypted_message = None
            record.completed_at = now
            record.claim_id = None
            record.lease_until = None
            db.commit()
            return True
    if record.attempts > MAX_ATTEMPTS:
        error = "delivery_attempts_exhausted"
    else:
        try:
            if record.encrypted_message is None:
                raise ValueError("missing delivery payload")
            message = BytesParser(policy=policy.default).parsebytes(
                cipher().decrypt(record.encrypted_message.encode())
            )
            if not message.get("Message-ID"):
                message["Message-ID"] = f"<invoxa-email-{record.id}@invoxa.invalid>"
            deliver(message, record.kind)
            error = None
        except Exception:
            error = "delivery_unavailable"
    terminal = error is not None and record.attempts >= MAX_ATTEMPTS
    db.expire_all()
    owned = db.scalar(
        select(TransactionalEmail)
        .where(
            TransactionalEmail.id == record.id,
            TransactionalEmail.claim_id == claim,
            TransactionalEmail.status == "sending",
        )
        .with_for_update()
    )
    if owned is None:
        db.rollback()
        return False
    if terminal and token is not None:
        if isinstance(token, OrganizationInvitation):
            token.status = OrganizationInvitationStatus.REVOKED
            token.revoked_at = now
        else:
            token.used_at = now
    db.execute(
        update(TransactionalEmail)
        .where(
            TransactionalEmail.id == record.id,
            TransactionalEmail.claim_id == claim,
            TransactionalEmail.status == "sending",
        )
        .values(
            status="failed" if terminal else "retry" if error else "sent",
            last_error=error,
            lease_until=None,
            claim_id=None,
            next_attempt_at=now + timedelta(seconds=min(3600, 30 * 2 ** (record.attempts - 1))),
            completed_at=datetime.now(UTC) if terminal or error is None else None,
            encrypted_message=None if terminal or error is None else record.encrypted_message,
        )
    )
    db.commit()
    return True
