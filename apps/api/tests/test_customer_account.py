"""Isolated account lifecycle and mandatory notification security checks."""

import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import get_current_user, hash_password
from apps.api.app.auth import router as auth_router
from apps.api.app.customer_account import router
from apps.api.app.database import Base, get_db
from apps.api.app.email_outbox import process_email
from apps.api.app.models import EmailChangeToken, OAuthIdentity, TransactionalEmail, User
from apps.api.app.rate_limit import get_rate_limiter
from apps.api.app.security_notifications import notify

PASSWORD = "Disposable-passphrase-123"


class AllowLimiter:
    def check(self, *args, **kwargs):
        pass


@pytest.fixture
def setup():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(email="old@example.com", password_hash=hash_password(PASSWORD))
        db.add(user)
        db.commit()
        application = FastAPI()
        application.include_router(router)
        application.include_router(auth_router)
        application.dependency_overrides[get_db] = lambda: db
        application.dependency_overrides[get_current_user] = lambda: user
        application.dependency_overrides[get_rate_limiter] = lambda: AllowLimiter()
        with TestClient(application) as client:
            yield db, user, client
    engine.dispose()


def request(client, address="new@example.com", password=PASSWORD):
    return client.post(
        "/auth/account/email-change", json={"new_email": address, "current_password": password}
    )


def raw_token(db):
    received = []
    assert process_email(db, lambda message, kind: received.append(message))
    link = next(word for word in received[0].get_content().split() if word.startswith("http"))
    return parse_qs(urlparse(link).query)["token"][0]


def test_profile_only_allowed_fields(setup):
    db, user, client = setup
    assert client.get("/auth/account/profile").status_code == 200
    response = client.patch(
        "/auth/account/profile",
        json={"display_name": " Demo User ", "email": "ignored@example.com"},
    )
    assert response.status_code == 200 and user.display_name == "Demo User"
    assert user.email == "old@example.com"
    assert (
        client.patch("/auth/account/profile", json={"display_name": "bad\x00name"}).status_code
        == 422
    )


def test_email_change_ownership_login_and_notifications(setup):
    db, user, client = setup
    assert request(client).status_code == 200
    assert user.email == "old@example.com"
    token = raw_token(db)
    row = db.scalar(select(EmailChangeToken))
    assert (
        row.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in row.token_hash
    )
    response = client.post("/auth/account/email-change/confirm", json={"token": token})
    assert response.status_code == 200
    assert user.email == "new@example.com" and user.auth_version == 1 and user.email_verified
    assert (
        client.post("/auth/account/email-change/confirm", json={"token": token}).status_code == 400
    )
    assert (
        client.post(
            "/auth/login", json={"email": "old@example.com", "password": PASSWORD}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login", json={"email": "new@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )
    notices = list(
        db.scalars(select(TransactionalEmail).where(TransactionalEmail.kind == "security-notice"))
    )
    assert len(notices) == 3  # old request + old/new completion


@pytest.mark.parametrize("condition", ["expired", "used", "password_changed", "inactive"])
def test_stale_email_change_rejected(setup, condition):
    db, user, client = setup
    assert request(client).status_code == 200
    token = raw_token(db)
    row = db.scalar(select(EmailChangeToken))
    if condition == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif condition == "used":
        row.used_at = datetime.now(UTC)
    elif condition == "password_changed":
        user.auth_version += 1
    else:
        user.is_active = False
    db.commit()
    assert (
        client.post("/auth/account/email-change/confirm", json={"token": token}).status_code == 400
    )
    assert user.email == "old@example.com"


def test_invalid_duplicate_cooldown_and_reauthentication(setup):
    db, user, client = setup
    assert request(client, password="wrong").status_code == 403
    assert (
        client.post("/auth/account/email-change/confirm", json={"token": "x" * 64}).status_code
        == 400
    )
    db.add(User(email="taken@example.com", password_hash="unused"))
    db.commit()
    assert request(client, "TAKEN@example.com").status_code == 409
    assert request(client).status_code == 200
    assert request(client, "another@example.com").status_code == 429
    assert user.email == "old@example.com"


def test_managed_identity_no_email_change(setup):
    db, user, client = setup
    db.add(OAuthIdentity(user_id=user.id, provider="google", subject="provider-user"))
    db.commit()
    assert client.get("/auth/account/profile").json()["identity_managed"]
    assert request(client).status_code == 403


@pytest.mark.parametrize("kind", ["saml", "scim"])
def test_enterprise_login_binding_not_mutated(setup, kind):
    import uuid

    from apps.api.app.models import SamlIdentity, ScimUserMapping

    db, user, client = setup
    # SQLite fixture has no external IdP; exercise the global-account binding boundary only.
    binding = (
        SamlIdentity(user_id=user.id, configuration_id=uuid.uuid4(), subject="stable")
        if kind == "saml"
        else ScimUserMapping(
            user_id=user.id,
            organization_id=uuid.uuid4(),
            user_name="mutable-scim-alias@example.com",
        )
    )
    db.add(binding)
    db.commit()
    assert request(client).status_code == 403
    assert user.email == "old@example.com" and binding.user_id == user.id


def test_notice_retry_terminal_failure_does_not_disable_account(setup):
    db, user, client = setup
    notify(db, user, "password_changed")
    db.commit()

    def fail(*args):
        raise RuntimeError("secret never stored in failure metadata")

    for attempt in range(1, 6):
        row = db.scalar(select(TransactionalEmail))
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert process_email(db, fail)
        db.refresh(row)
        assert row.attempts == attempt
        assert row.status == ("failed" if attempt == 5 else "retry")
        assert row.last_error == "delivery_unavailable"
    assert row.encrypted_message is None and user.is_active


def test_password_reset_queues_mandatory_notice(setup):
    from apps.api.app.models import PasswordResetToken

    db, user, client = setup
    raw = "recipient-only-reset-" + "x" * 48
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
        )
    )
    db.commit()
    response = client.post(
        "/auth/reset-password", json={"token": raw, "new_password": "New-disposable-password-123"}
    )
    assert response.status_code == 200
    rows = list(db.scalars(select(TransactionalEmail)))
    assert len(rows) == 1 and rows[0].kind == "security-notice"
    assert rows[0].status == "pending" and user.auth_version == 1
    assert (
        client.post(
            "/auth/reset-password",
            json={"token": raw, "new_password": "New-disposable-password-123"},
        ).status_code
        == 400
    )
    assert len(list(db.scalars(select(TransactionalEmail)))) == 1


def test_notification_idempotency_and_enqueue_failure(setup, monkeypatch):
    import uuid

    import apps.api.app.security_notifications as notices

    db, user, client = setup
    event = uuid.uuid4()
    assert notify(db, user, "sessions_revoked", event_id=event)
    assert notify(db, user, "sessions_revoked", event_id=event)
    db.commit()
    assert len(list(db.scalars(select(TransactionalEmail)))) == 1

    def fail(*args, **kwargs):
        raise RuntimeError("secret-bearing backend error")

    monkeypatch.setattr(notices, "enqueue", fail)
    response = client.post(
        "/auth/account/password",
        json={"current_password": PASSWORD, "new_password": "New-disposable-password-123"},
    )
    assert response.status_code == 200 and user.auth_version == 1
