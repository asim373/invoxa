from email.message import EmailMessage

import pytest
from pydantic import SecretStr

from apps.api.app import email as email_module
from apps.api.app.settings import settings


def test_smtp_sender_uses_configured_starttls_auth_and_sender(monkeypatch):
    events = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            events.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def starttls(self):
            events.append(("starttls",))

        def login(self, username, password):
            events.append(("login", username, password))

        def send_message(self, message):
            events.append(("send", message["From"], message["To"], message.get_content()))

    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "smtp_host", "smtp.test.invalid")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_username", "qa-smtp-user")
    monkeypatch.setattr(settings, "smtp_password", SecretStr("synthetic-mail-secret"))
    monkeypatch.setattr(settings, "smtp_from_address", "no-reply@example.test")
    monkeypatch.setattr(settings, "smtp_starttls", True)
    monkeypatch.setattr(settings, "smtp_use_ssl", False)
    monkeypatch.setattr(email_module.smtplib, "SMTP", FakeSMTP)

    email_module.ConfiguredInvitationEmailSender().send_invitation(
        "member@example.test", "https://app.example.test/invite/accept?token=synthetic", "QA"
    )

    assert events[0] == ("connect", "smtp.test.invalid", 587, 10)
    assert events[1] == ("starttls",)
    assert events[2] == ("login", "qa-smtp-user", "synthetic-mail-secret")
    assert events[3][1:3] == ("no-reply@example.test", "member@example.test")
    assert "https://app.example.test/invite/accept?token=synthetic" in events[3][3]


def test_production_email_fails_closed_without_smtp_and_never_writes_outbox(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "app_environment", "production")
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "password_reset_dev_outbox_path", tmp_path / "outbox")
    with pytest.raises(RuntimeError, match="Email delivery is not configured"):
        email_module._deliver_or_outbox(EmailMessage(), "synthetic-test")
    assert not (tmp_path / "outbox").exists()
