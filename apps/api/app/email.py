import hashlib
import logging
import smtplib
import uuid
from email.message import EmailMessage
from typing import Annotated, Protocol

from fastapi import Depends
from sqlalchemy.orm import Session

from apps.api.app.database import get_db
from apps.api.app.settings import settings

logger = logging.getLogger(__name__)


class PasswordResetEmailSender(Protocol):
    def send_password_reset(self, recipient: str, reset_url: str) -> None: ...


class InvitationEmailSender(Protocol):
    def send_invitation(
        self, recipient: str, invitation_url: str, organization_name: str
    ) -> None: ...


def _deliver_or_outbox(message: EmailMessage, kind: str) -> None:
    if settings.smtp_host:
        smtp_type = smtplib.SMTP_SSL if settings.smtp_use_ssl else smtplib.SMTP
        with smtp_type(settings.smtp_host, settings.smtp_port, timeout=10) as client:
            if settings.smtp_starttls:
                client.starttls()
            if settings.smtp_username:
                client.login(
                    settings.smtp_username,
                    settings.smtp_password.get_secret_value() if settings.smtp_password else "",
                )
            client.send_message(message)
        return
    if settings.app_environment.casefold() not in {"development", "test"}:
        raise RuntimeError("Email delivery is not configured.")
    outbox = settings.password_reset_dev_outbox_path.resolve()
    outbox.mkdir(parents=True, exist_ok=True)
    message_id = message.get("Message-ID")
    safe_id = (
        hashlib.sha256(str(message_id).encode()).hexdigest() if message_id else uuid.uuid4().hex
    )
    destination = outbox / f"{kind}-{safe_id}.eml"
    destination.write_bytes(message.as_bytes())
    logger.info("email_written_to_development_outbox type=%s", kind)


class ConfiguredPasswordResetEmailSender:
    def __init__(self, database: Session | None = None):
        self.database = database

    def send_password_reset(self, recipient: str, reset_url: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Reset your Document & Invoice Analyzer password"
        message["From"] = settings.smtp_from_address or "no-reply@localhost"
        message["To"] = recipient
        message.set_content(
            "A password reset was requested for your account.\n\n"
            f"Reset your password: {reset_url}\n\n"
            f"This link expires in {settings.password_reset_token_expire_minutes} minutes. "
            "If you did not request this, you can ignore this email."
        )

        if self.database is None:
            _deliver_or_outbox(message, "password-reset")
        else:
            from apps.api.app.email_outbox import enqueue_link

            enqueue_link(self.database, message, "password-reset", reset_url)


def get_password_reset_email_sender(
    database: Annotated[Session, Depends(get_db)],
) -> PasswordResetEmailSender:
    return ConfiguredPasswordResetEmailSender(database)


class ConfiguredInvitationEmailSender:
    queued = True

    def __init__(self, database: Session | None = None):
        self.database = database

    def send_invitation(self, recipient: str, invitation_url: str, organization_name: str) -> None:
        message = EmailMessage()
        message["Subject"] = f"Invitation to join {organization_name}"
        message["From"] = settings.smtp_from_address or "no-reply@localhost"
        message["To"] = recipient
        message.set_content(
            f"You have been invited to join {organization_name}.\n\n"
            f"Accept the invitation: {invitation_url}\n\nThis link expires in 7 days."
        )
        if self.database is None:
            _deliver_or_outbox(message, "invitation")
        else:
            from apps.api.app.email_outbox import enqueue_link

            enqueue_link(self.database, message, "invitation", invitation_url)


def get_invitation_email_sender(
    database: Annotated[Session, Depends(get_db)],
) -> InvitationEmailSender:
    return ConfiguredInvitationEmailSender(database)


class VerificationEmailSender(Protocol):
    def send_verification(self, recipient: str, verification_url: str) -> None: ...


class ConfiguredVerificationEmailSender:
    def __init__(self, database: Session | None = None):
        self.database = database

    def send_verification(self, recipient: str, verification_url: str) -> None:
        message = EmailMessage()
        message["Subject"] = "Verify your Invoxa email address"
        message["From"] = settings.smtp_from_address or "no-reply@localhost"
        message["To"] = recipient
        message.set_content(
            f"Verify your email address: {verification_url}\n\n"
            "This single-use link expires in 60 minutes."
        )
        if self.database is None:
            _deliver_or_outbox(message, "email-verification")
        else:
            from apps.api.app.email_outbox import enqueue_link

            enqueue_link(self.database, message, "email-verification", verification_url)


def get_verification_email_sender(
    database: Annotated[Session, Depends(get_db)],
) -> VerificationEmailSender:
    return ConfiguredVerificationEmailSender(database)
