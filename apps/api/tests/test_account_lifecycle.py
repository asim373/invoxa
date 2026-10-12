"""No persistent database: account deactivation preserves business records."""

import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.account_lifecycle import router
from apps.api.app.auth import hash_password
from apps.api.app.auth import router as auth_router
from apps.api.app.database import Base, get_db
from apps.api.app.email_outbox import process_email
from apps.api.app.models import (
    AccountDeletionTombstone,
    AccountReactivationToken,
    Document,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.provisioning import provision_company
from apps.api.app.rate_limit import get_rate_limiter

PASSWORD = "Disposable-lifecycle-123"


@pytest.fixture
def setup():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(email="lifecycle@example.com", password_hash=hash_password(PASSWORD))
        db.add(user)
        db.commit()
        application = FastAPI()
        application.include_router(auth_router)
        application.include_router(router)
        application.dependency_overrides[get_db] = lambda: db

        class Limiter:
            def check(self, *args, **kwargs):
                pass

        application.dependency_overrides[get_rate_limiter] = lambda: Limiter()
        with TestClient(application) as client:
            token = client.post(
                "/auth/login", json={"email": user.email, "password": PASSWORD}
            ).json()["access_token"]
            yield db, user, client, {"Authorization": "Bearer " + token}
    engine.dispose()


def deactivate(client, headers, password=PASSWORD, confirmation="DEACTIVATE"):
    return client.post(
        "/auth/account/deactivate",
        headers=headers,
        json={"current_password": password, "confirmation": confirmation},
    )


def recovery_token(db, user, client):
    response = client.post(
        "/auth/account/reactivation-request",
        json={"email": user.email, "current_password": PASSWORD},
    )
    assert response.status_code == 200
    received = []
    while process_email(db, lambda message, kind: received.append((message, kind))):
        pass
    mail = next(m for m, kind in received if kind == "account-reactivation")
    link = next(word for word in mail.get_content().split() if word.startswith("http"))
    return parse_qs(urlparse(link).query)["token"][0]


def test_deactivate_recover_revoked_sessions_stay_invalid(setup):
    db, user, client, headers = setup
    assert deactivate(client, headers).status_code == 200
    assert not user.is_active and user.self_deactivated_at is not None
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert (
        client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 401
    )
    raw = recovery_token(db, user, client)
    row = db.scalar(select(AccountReactivationToken))
    assert row.token_hash == hashlib.sha256(raw.encode()).hexdigest()
    payload = {"token": raw, "current_password": PASSWORD}
    assert client.post("/auth/account/reactivate", json=payload).status_code == 200
    assert user.is_active and user.self_deactivated_at is None and user.auth_version == 2
    assert client.post("/auth/account/reactivate", json=payload).status_code == 400
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert (
        client.post("/auth/login", json={"email": user.email, "password": PASSWORD}).status_code
        == 200
    )


def test_confirmation_password_and_last_owner_protection(setup):
    db, user, client, headers = setup
    assert deactivate(client, headers, password="wrong").status_code == 403
    assert deactivate(client, headers, confirmation="no").status_code == 422
    org = provision_company(db, user=user, company_name="Preserved Company")
    db.commit()
    assert deactivate(client, headers).status_code == 409
    assert user.is_active and db.get(type(org), org.id) is not None
    second = User(email="second-owner@example.com", password_hash="unused")
    db.add(second)
    db.flush()
    db.add(
        OrganizationMember(
            user_id=second.id,
            organization_id=org.id,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    db.commit()
    assert deactivate(client, headers).status_code == 200
    assert db.get(type(org), org.id) is not None


@pytest.mark.parametrize("condition", ["expired", "wrong_password", "version_changed"])
def test_recovery_security_failures(setup, condition):
    db, user, client, headers = setup
    assert deactivate(client, headers).status_code == 200
    raw = recovery_token(db, user, client)
    row = db.scalar(select(AccountReactivationToken))
    if condition == "expired":
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    if condition == "version_changed":
        user.auth_version += 1
    db.commit()
    assert (
        client.post(
            "/auth/account/reactivate",
            json={
                "token": raw,
                "current_password": "wrong" if condition == "wrong_password" else PASSWORD,
            },
        ).status_code
        == 400
    )
    assert not user.is_active


def test_administratively_disabled_account_cannot_self_reactivate(setup):
    db, user, client, headers = setup
    user.is_active = False
    db.commit()
    assert (
        client.post(
            "/auth/account/reactivation-request",
            json={"email": user.email, "current_password": PASSWORD},
        ).status_code
        == 200
    )
    assert db.scalar(select(AccountReactivationToken)) is None


def test_permanent_deletion_anonymizes_identity_and_revokes_access(setup):
    db, user, client, headers = setup
    original = user.email
    response = client.post(
        "/auth/account/delete",
        headers=headers,
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )
    assert response.status_code == 200
    assert not user.is_active and user.deleted_at is not None
    assert user.email != original and user.email.endswith("@deleted.invoxa.invalid")
    assert db.scalar(
        select(AccountDeletionTombstone).where(AccountDeletionTombstone.user_id == user.id)
    )
    assert client.get("/auth/me", headers=headers).status_code == 401
    assert (
        client.post("/auth/login", json={"email": original, "password": PASSWORD}).status_code
        == 401
    )


def test_deletion_preserves_company_documents_and_blocks_last_owner(setup):
    db, user, client, headers = setup
    org = provision_company(db, user=user, company_name="Company data survives")
    document = Document(
        original_filename="shared.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="0" * 64,
        storage_location="shared.pdf",
        owner_id=user.id,
        created_by_user_id=user.id,
        organization_id=org.id,
    )
    db.add(document)
    db.commit()
    denied = client.post(
        "/auth/account/delete",
        headers=headers,
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )
    assert denied.status_code == 409
    second = User(email="deletion-owner@example.com", password_hash="unused")
    db.add(second)
    db.flush()
    db.add(
        OrganizationMember(
            user_id=second.id,
            organization_id=org.id,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    db.commit()
    assert (
        client.post(
            "/auth/account/delete",
            headers=headers,
            json={"current_password": PASSWORD, "confirmation": "DELETE"},
        ).status_code
        == 200
    )
    assert db.get(Document, document.id) is not None


def test_deletion_rejects_wrong_confirmation_and_managed_identity(setup):
    db, user, client, headers = setup
    assert (
        client.post(
            "/auth/account/delete",
            headers=headers,
            json={"current_password": PASSWORD, "confirmation": "no"},
        ).status_code
        == 422
    )
    # A SAML/SCIM/OAuth-managed identity must follow the administrator/IdP path,
    # not the password self-service deletion path.
    from apps.api.app.models import OAuthIdentity

    db.add(OAuthIdentity(user_id=user.id, provider="google", subject="subject"))
    db.commit()
    assert (
        client.post(
            "/auth/account/delete",
            headers=headers,
            json={"current_password": PASSWORD, "confirmation": "DELETE"},
        ).status_code
        == 403
    )


def test_deletion_removes_exclusively_personal_document(setup, tmp_path, monkeypatch):
    from apps.api.app.settings import settings

    db, user, client, headers = setup
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    file_path = tmp_path / "personal.pdf"
    file_path.write_bytes(b"disposable personal file")
    document = Document(
        original_filename="personal.pdf",
        mime_type="application/pdf",
        file_size=file_path.stat().st_size,
        checksum="1" * 64,
        storage_location="personal.pdf",
        owner_id=user.id,
    )
    db.add(document)
    db.commit()
    assert (
        client.post(
            "/auth/account/delete",
            headers=headers,
            json={"current_password": PASSWORD, "confirmation": "DELETE"},
        ).status_code
        == 200
    )
    assert db.get(Document, document.id) is None
    assert not file_path.exists()
