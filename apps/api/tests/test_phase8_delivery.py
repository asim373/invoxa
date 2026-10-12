"""Phase 8 checks exclusively use a disposable in-memory database."""

import hashlib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base, get_db
from apps.api.app.email import ConfiguredVerificationEmailSender
from apps.api.app.email_outbox import cipher, enqueue, process_email
from apps.api.app.email_verification import issue_verification, router
from apps.api.app.models import EmailVerificationToken, TransactionalEmail, User
from apps.api.app.rate_limit import get_rate_limiter


class AllowLimiter:
    def check(self, *args, **kwargs):
        pass


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture
def client(db):
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_rate_limiter] = lambda: AllowLimiter()
    with TestClient(application) as test_client:
        yield test_client


def message():
    value = EmailMessage()
    value["From"] = "sender@example.com"
    value["To"] = "recipient@example.com"
    value["Subject"] = "Disposable test"
    value.set_content("Recipient-only verification secret")
    return value


def test_outbox_encryption_idempotency_and_success(db):
    mail = message()
    record = enqueue(db, mail, "test")
    assert enqueue(db, mail, "test").id == record.id
    assert "Recipient-only" not in record.encrypted_message
    assert cipher().decrypt(record.encrypted_message.encode()) == mail.as_bytes()
    db.commit()
    received = []
    assert process_email(db, lambda value, kind: received.append((value, kind)))
    db.refresh(record)
    assert record.status == "sent" and record.attempts == 1
    assert record.encrypted_message is None and record.completed_at is not None
    assert len(received) == 1
    assert not process_email(db, lambda *args: pytest.fail("Duplicate send"))


def test_retry_backoff_terminal_failure_and_safe_errors(db):
    record = enqueue(db, message(), "test")
    db.commit()

    def unavailable(*args):
        raise RuntimeError("secret-provider-password")

    for attempt in range(1, 6):
        before = datetime.now(UTC)
        assert process_email(db, unavailable)
        db.refresh(record)
        assert record.attempts == attempt
        assert record.last_error == "delivery_unavailable"
        assert "secret" not in record.last_error
        if attempt < 5:
            assert record.status == "retry"
            scheduled = record.next_attempt_at.replace(tzinfo=UTC)
            assert (scheduled - before).total_seconds() >= 30 * 2 ** (attempt - 1) - 1
            record.next_attempt_at = before - timedelta(seconds=1)
            db.commit()
        else:
            assert record.status == "failed" and record.encrypted_message is None
    assert not process_email(db, unavailable)


def test_abandoned_claim_recovery(db):
    record = enqueue(db, message(), "test")
    record.status = "sending"
    record.attempts = 1
    record.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    db.commit()
    assert process_email(db, lambda *args: None)
    db.refresh(record)
    assert record.status == "sent" and record.attempts == 2


def test_active_claim_is_not_taken(db):
    record = enqueue(db, message(), "test")
    record.status = "sending"
    record.lease_until = datetime.now(UTC) + timedelta(minutes=1)
    db.commit()
    assert not process_email(db, lambda *args: pytest.fail("Active claim stolen"))


def test_verification_issues_only_hash_and_consumes_once(db, client):
    user = User(email="pending@example.com", password_hash="unused", email_verified=False)
    db.add(user)
    db.flush()
    captured = []

    class Sender:
        def send_verification(self, recipient, url):
            from urllib.parse import parse_qs, urlparse

            captured.append(parse_qs(urlparse(url).query)["token"][0])

    issue_verification(db, user, Sender())
    db.commit()
    raw = captured[0]
    record = db.scalar(select(EmailVerificationToken))
    assert record.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    assert raw != record.token_hash
    assert client.post("/auth/verify-email", json={"token": raw}).status_code == 200
    db.refresh(user)
    assert user.email_verified
    assert client.post("/auth/verify-email", json={"token": raw}).status_code == 400


@pytest.mark.parametrize("state", ["invalid", "expired", "used", "inactive"])
def test_invalid_verification_rejected(db, client, state):
    raw = "disposable-verification-value-long-enough"
    user = User(
        email="invalid@example.com",
        password_hash="unused",
        email_verified=False,
        is_active=state != "inactive",
    )
    db.add(user)
    db.flush()
    if state != "invalid":
        db.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(minutes=-1 if state == "expired" else 60),
                used_at=datetime.now(UTC) if state == "used" else None,
            )
        )
    db.commit()
    assert client.post("/auth/verify-email", json={"token": raw}).status_code == 400
    db.refresh(user)
    assert not user.email_verified


def test_resend_cooldown_and_generic_response(db, client):
    user = User(email="resend@example.com", password_hash="unused", email_verified=False)
    db.add(user)
    db.flush()
    issue_verification(db, user, ConfiguredVerificationEmailSender(db))
    db.commit()
    first = client.post("/auth/resend-verification", json={"email": user.email})
    unknown = client.post("/auth/resend-verification", json={"email": "unknown@example.com"})
    assert first.status_code == 200 and first.json() == unknown.json()
    assert len(list(db.scalars(select(TransactionalEmail)))) == 1


def test_legacy_accounts_remain_verified(db):
    user = User(email="legacy@example.com", password_hash="unused")
    db.add(user)
    db.commit()
    assert user.email_verified is True


def test_worker_outbox_filenames_are_delivery_unique(db, monkeypatch, tmp_path):
    from apps.api.app.settings import settings

    monkeypatch.setattr(settings, "app_environment", "test")
    monkeypatch.setattr(settings, "smtp_host", None)
    monkeypatch.setattr(settings, "password_reset_dev_outbox_path", tmp_path)
    first = message()
    second = message()
    second.replace_header("Subject", "Second disposable delivery")
    enqueue(db, first, "test")
    enqueue(db, second, "test")
    db.commit()
    assert process_email(db) and process_email(db)
    assert len(list(tmp_path.glob("test-*.eml"))) == 2


def test_invitation_is_queued_delivered_and_acceptance_remains_verified(db):
    from urllib.parse import parse_qs, urlparse

    from apps.api.app.auth import create_access_token
    from apps.api.app.auth import router as auth_router
    from apps.api.app.models import (
        Organization,
        OrganizationMember,
        OrganizationMemberRole,
        OrganizationMemberStatus,
    )
    from apps.api.app.plans import PROFESSIONAL_PLAN_CODE
    from apps.api.app.team import router as team_router

    owner = User(email="invite-owner@example.com", password_hash="unused")
    organization = Organization(
        name="Disposable Invite Company",
        slug="phase8-invite-company",
        plan_code=PROFESSIONAL_PLAN_CODE,
    )
    db.add(
        OrganizationMember(
            user=owner,
            organization=organization,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    db.commit()
    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(team_router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_rate_limiter] = lambda: AllowLimiter()
    headers = {
        "Authorization": "Bearer " + create_access_token(owner.id),
        "X-Organization-ID": str(organization.id),
    }
    with TestClient(application) as client:
        created = client.post(
            "/team/invitations",
            headers=headers,
            json={"email": "invite-recipient@example.com", "role": "viewer"},
        )
        assert created.status_code == 201
        delivery = db.scalar(select(TransactionalEmail))
        assert delivery.organization_id == organization.id and delivery.status == "pending"
        received = []
        assert process_email(db, lambda mail, kind: received.append(mail))
        link = received[0].get_content().split("Accept the invitation: ", 1)[1].split()[0]
        raw = parse_qs(urlparse(link).query)["token"][0]
        accepted = client.post(
            "/team/invitations/accept-new",
            json={
                "token": raw,
                "email": "invite-recipient@example.com",
                "password": "Disposable-invite-password-123",
            },
        )
        assert accepted.status_code == 201
        invitee = db.scalar(select(User).where(User.email == "invite-recipient@example.com"))
        assert invitee.email_verified is True


def test_real_smtp_retry_and_recovery(db, monkeypatch):
    from apps.api.app.settings import settings
    from apps.api.tests.phase8_smtp_sink import smtp_sink

    record = enqueue(db, message(), "test")
    db.commit()
    with smtp_sink(failures=1) as (port, received):
        monkeypatch.setattr(settings, "smtp_host", "127.0.0.1")
        monkeypatch.setattr(settings, "smtp_port", port)
        monkeypatch.setattr(settings, "smtp_starttls", False)
        monkeypatch.setattr(settings, "smtp_use_ssl", False)
        monkeypatch.setattr(settings, "smtp_username", None)
        assert process_email(db)
        db.refresh(record)
        assert record.status == "retry" and record.attempts == 1 and not received
        record.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert process_email(db)
        db.refresh(record)
        assert record.status == "sent" and record.attempts == 2 and len(received) == 1


def test_delivery_visibility_is_user_scoped_and_payload_free(db):
    from apps.api.app.auth import get_current_user
    from apps.api.app.email_delivery_api import router as delivery_router

    first = User(email="first@example.com", password_hash="unused")
    second = User(email="second@example.com", password_hash="unused")
    db.add_all([first, second])
    db.flush()
    enqueue(db, message(), "first", user_id=first.id)
    enqueue(db, message(), "second", user_id=second.id)
    db.commit()
    application = FastAPI()
    application.include_router(delivery_router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_current_user] = lambda: first
    with TestClient(application) as client:
        response = client.get("/auth/email-deliveries")
        assert response.status_code == 200
        assert len(response.json()) == 1 and response.json()[0]["kind"] == "first"
        assert "encrypted_message" not in response.text
        assert "recipient" not in response.text


def test_signup_verification_login_and_workspace_access(db):
    from email import policy
    from email.parser import BytesParser
    from urllib.parse import parse_qs, urlparse

    from apps.api.app.auth import router as auth_router

    application = FastAPI()
    application.include_router(auth_router)
    application.include_router(router)
    application.dependency_overrides[get_db] = lambda: db
    application.dependency_overrides[get_rate_limiter] = lambda: AllowLimiter()
    credentials = {"email": "signup@example.com", "password": "Disposable-password-only-123"}
    with TestClient(application) as client:
        registration = client.post(
            "/auth/register", json={**credentials, "company_name": "Disposable Phase8 Company"}
        )
        assert registration.status_code == 201
        assert registration.json()["email_verified"] is False
        assert client.post("/auth/login", json=credentials).status_code == 403
        delivery = db.scalar(select(TransactionalEmail))
        assert delivery.status == "pending" and delivery.attempts == 0
        received = []
        assert process_email(db, lambda mail, kind: received.append(mail))
        mail = received[0]
        assert isinstance(mail, type(BytesParser(policy=policy.default).parsebytes(b"")))
        link = mail.get_content().split("Verify your email address: ", 1)[1].split()[0]
        raw = parse_qs(urlparse(link).query)["token"][0]
        assert client.post("/auth/verify-email", json={"token": raw}).status_code == 200
        login = client.post("/auth/login", json=credentials)
        assert login.status_code == 200
        me = client.get(
            "/auth/me", headers={"Authorization": "Bearer " + login.json()["access_token"]}
        )
        assert me.status_code == 200 and me.json()["email_verified"] is True


def test_outbox_migration_upgrade_downgrade(db):
    import importlib

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect

    migration = importlib.import_module(
        "apps.api.migrations.versions.0032_transactional_email_outbox"
    )
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert "transactional_emails" in inspect(connection).get_table_names()
            assert {
                index["name"] for index in inspect(connection).get_indexes("transactional_emails")
            } == {
                "ix_transactional_emails_due",
                "ix_transactional_emails_user_id",
                "ix_transactional_emails_organization_id",
            }
            migration.downgrade()
            assert "transactional_emails" not in inspect(connection).get_table_names()
    engine.dispose()


def test_terminal_failure_invalidates_verification_token(db):
    user = User(email="terminal@example.com", password_hash="unused", email_verified=False)
    db.add(user)
    db.flush()
    issue_verification(db, user, ConfiguredVerificationEmailSender(db))
    db.commit()
    delivery = db.scalar(select(TransactionalEmail))
    delivery.attempts = 4
    db.commit()

    def unavailable(*args):
        raise OSError("Synthetic unavailable provider")

    assert process_email(db, unavailable)
    db.refresh(delivery)
    token = db.get(EmailVerificationToken, delivery.related_token_id)
    assert delivery.status == "failed" and token.used_at is not None
    assert delivery.encrypted_message is None


def test_superseded_verification_email_is_not_sent(db):
    user = User(email="superseded@example.com", password_hash="unused", email_verified=False)
    db.add(user)
    db.flush()
    sender = ConfiguredVerificationEmailSender(db)
    issue_verification(db, user, sender)
    db.commit()
    first = db.scalar(select(TransactionalEmail))
    issue_verification(db, user, sender)
    db.commit()
    assert process_email(db, lambda *args: pytest.fail("Superseded token sent"))
    db.refresh(first)
    assert first.status == "cancelled" and first.encrypted_message is None


def test_verification_rate_limit_is_enforced(db, client):
    from fastapi import HTTPException

    class RejectLimiter:
        def check(self, *args, **kwargs):
            assert kwargs["fail_closed"] is True
            raise HTTPException(429, "Too many requests")

    client.app.dependency_overrides[get_rate_limiter] = lambda: RejectLimiter()
    assert (
        client.post("/auth/resend-verification", json={"email": "limited@example.com"}).status_code
        == 429
    )
    assert (
        client.post(
            "/auth/verify-email", json={"token": "disposable-token-long-enough-for-validation"}
        ).status_code
        == 429
    )
