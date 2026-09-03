import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from apps.api.app.settings import settings

logger = logging.getLogger(__name__)


class PasswordResetEmailSender(Protocol):
    def send_password_reset(self, recipient: str, reset_url: str) -> None: ...


class ConfiguredPasswordResetEmailSender:
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

        if settings.app_environment.casefold() == "production":
            raise RuntimeError("Password reset email delivery is not configured.")

        outbox = settings.password_reset_dev_outbox_path.resolve()
        outbox.mkdir(parents=True, exist_ok=True)
        destination = outbox / f"password-reset-{message['Message-ID'] or id(message)}.eml"
        destination.write_bytes(message.as_bytes())
        logger.info("password_reset_email_written_to_development_outbox")


def get_password_reset_email_sender() -> PasswordResetEmailSender:
    return ConfiguredPasswordResetEmailSender()
