# ruff: noqa: E501

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base, get_db
from apps.api.app.google_auth import (
    GoogleIdentity,
    _accept_google_invitation,
    create_google_onboarding_token,
)
from apps.api.app.main import app
from apps.api.app.models import (
    OAuthIdentity,
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.plans import DEFAULT_PLAN_CODE
from apps.api.app.settings import settings


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            app.dependency_overrides[get_db] = lambda: session
            yield session
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def onboarding_token(subject: str = "google-subject", email: str = "google@example.com") -> str:
    return create_google_onboarding_token(GoogleIdentity(subject=subject, email=email))


def test_google_onboarding_provisions_owner_and_exchange_token(database: Session) -> None:
    client = TestClient(app)
    response = client.post(
        "/auth/google/complete-onboarding",
        json={"onboarding_token": onboarding_token(), "company_name": "Google Workspace"},
    )

    assert response.status_code == 200
    body = response.json()
    user = database.scalar(select(User).where(User.email == "google@example.com"))
    organization = database.scalar(select(Organization))
    membership = database.scalar(select(OrganizationMember))
    identity = database.scalar(select(OAuthIdentity))
    assert user is not None and organization is not None and membership is not None
    assert organization.plan_code == DEFAULT_PLAN_CODE
    assert identity is not None
    assert membership.user_id == user.id
    assert membership.organization_id == organization.id
    assert membership.role.value == "owner"
    assert membership.status.value == "active"
    assert body["organization_id"] == str(organization.id)

    exchanged = client.post("/auth/google/exchange", json={"code": body["google_code"]})
    assert exchanged.status_code == 200
    tenant_access = client.get(
        "/documents",
        headers={
            "Authorization": f"Bearer {exchanged.json()['access_token']}",
            "X-Organization-ID": body["organization_id"],
        },
    )
    assert tenant_access.status_code == 200


def test_google_onboarding_token_is_tamper_and_expiry_resistant(database: Session) -> None:
    client = TestClient(app)
    original = onboarding_token()
    header, payload, signature = original.split(".")
    payload = payload[:-1] + ("a" if payload[-1] != "a" else "b")
    tampered = ".".join((header, payload, signature))
    expired = jwt.encode(
        {
            "kind": "google_company_onboarding",
            "sub": "expired",
            "email": "expired@example.com",
            "jti": "expired-jti",
            "iat": datetime.now(UTC) - timedelta(minutes=20),
            "exp": datetime.now(UTC) - timedelta(minutes=10),
        },
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
    )

    for token in (tampered, expired):
        response = client.post(
            "/auth/google/complete-onboarding",
            json={"onboarding_token": token, "company_name": "Invalid Workspace"},
        )
        assert response.status_code == 400
    assert database.query(User).count() == 0


def test_google_onboarding_replay_and_client_identity_substitution_are_rejected(
    database: Session,
) -> None:
    client = TestClient(app)
    token = onboarding_token()
    first = client.post(
        "/auth/google/complete-onboarding",
        json={
            "onboarding_token": token,
            "company_name": "One Workspace",
            "email": "attacker@example.com",
            "role": "admin",
            "organization_id": "00000000-0000-0000-0000-000000000001",
        },
    )
    replay = client.post(
        "/auth/google/complete-onboarding",
        json={"onboarding_token": token, "company_name": "Two Workspace"},
    )

    assert first.status_code == 200
    assert replay.status_code == 409
    assert database.query(User).count() == 1
    assert database.query(Organization).count() == 1
    assert database.query(OrganizationMember).count() == 1


def test_google_onboarding_failure_rolls_back_commercial_records(database: Session) -> None:
    client = TestClient(app)
    response = client.post(
        "/auth/google/complete-onboarding",
        json={"onboarding_token": onboarding_token(), "company_name": "   "},
    )

    assert response.status_code == 422
    assert database.query(User).count() == 0
    assert database.query(Organization).count() == 0
    assert database.query(OrganizationMember).count() == 0


def test_new_google_invitee_joins_existing_workspace_without_onboarding(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", settings.auth_secret_key)
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "http://testserver/auth/google/callback")
    owner = User(email="owner@example.com", password_hash="hash")
    organization = Organization(name="Atlas", slug="atlas", plan_code=DEFAULT_PLAN_CODE)
    database.add(OrganizationMember(organization=organization, user=owner, role=OrganizationMemberRole.OWNER, status=OrganizationMemberStatus.ACTIVE))
    database.flush()
    raw_invitation = secrets.token_urlsafe(48)
    database.add(OrganizationInvitation(
        organization_id=organization.id,
        invited_email="new-google@example.com",
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationInvitationStatus.PENDING,
        token_hash=hashlib.sha256(raw_invitation.encode()).hexdigest(),
        invited_by_user_id=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    ))
    database.commit()
    owner_membership = database.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == owner.id)
    )
    assert owner_membership is not None
    owner_membership.status = OrganizationMemberStatus.INACTIVE
    database.commit()
    monkeypatch.setattr("apps.api.app.google_auth.exchange_google_code", lambda code, verifier: "id-token")
    monkeypatch.setattr("apps.api.app.google_auth.verify_google_id_token", lambda token, nonce: GoogleIdentity(subject="new-subject", email="new-google@example.com"))
    client = TestClient(app)
    start = client.get(f"/auth/google/start?invitation_token={raw_invitation}", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(f"/auth/google/callback?state={state}&code=provider-code", follow_redirects=False)
    assert callback.status_code == 303
    query = parse_qs(urlparse(callback.headers["location"]).query)
    assert query["google_invitation"] == ["1"]
    assert query["organization_id"] == [str(organization.id)]
    assert database.query(Organization).count() == 1
    user = database.scalar(select(User).where(User.email == "new-google@example.com"))
    assert user is not None
    membership = database.scalar(select(OrganizationMember).where(OrganizationMember.user_id == user.id))
    assert membership is not None
    assert membership.organization_id == organization.id
    assert membership.role is OrganizationMemberRole.MEMBER
    invitation = database.scalar(select(OrganizationInvitation))
    assert invitation is not None
    assert invitation.status is OrganizationInvitationStatus.ACCEPTED


def test_google_invitation_acceptance_enforces_active_seat_limit(database: Session) -> None:
    owner = User(email="google-seat-owner@example.com", password_hash="hash")
    organization = Organization(
        name="Google Seat", slug="google-seat", plan_code=DEFAULT_PLAN_CODE
    )
    database.add(
        OrganizationMember(
            organization=organization,
            user=owner,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    database.flush()
    raw_invitation = secrets.token_urlsafe(48)
    database.add(
        OrganizationInvitation(
            organization=organization,
            invited_email="google-seat-new@example.com",
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationInvitationStatus.PENDING,
            token_hash=hashlib.sha256(raw_invitation.encode()).hexdigest(),
            invited_by_user_id=owner.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    database.commit()
    for index in range(4):
        member = User(email=f"google-seat-{index}@example.com", password_hash="hash")
        database.add(
            OrganizationMember(
                organization=organization,
                user=member,
                role=OrganizationMemberRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
            )
        )
    database.commit()

    with pytest.raises(HTTPException) as error:
        _accept_google_invitation(
            database,
            raw_invitation,
            GoogleIdentity(subject="google-seat-subject", email="google-seat-new@example.com"),
        )
    assert error.value.status_code == 409
    database.rollback()
    invitation = database.scalar(select(OrganizationInvitation))
    assert invitation is not None
    assert invitation.status is OrganizationInvitationStatus.PENDING
    assert database.scalar(select(User).where(User.email == "google-seat-new@example.com")) is None


def test_google_invitation_email_mismatch_does_not_consume_invitation(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", settings.auth_secret_key)
    monkeypatch.setattr(settings, "google_oauth_redirect_uri", "http://testserver/auth/google/callback")
    owner = User(email="owner2@example.com", password_hash="hash")
    organization = Organization(name="Atlas 2", slug="atlas-2")
    database.add(OrganizationMember(organization=organization, user=owner, role=OrganizationMemberRole.OWNER, status=OrganizationMemberStatus.ACTIVE))
    database.flush()
    raw_invitation = secrets.token_urlsafe(48)
    invitation = OrganizationInvitation(
        organization_id=organization.id,
        invited_email="alice@example.com",
        role=OrganizationMemberRole.MEMBER,
        token_hash=hashlib.sha256(raw_invitation.encode()).hexdigest(),
        invited_by_user_id=owner.id,
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    database.add(invitation)
    database.commit()
    monkeypatch.setattr("apps.api.app.google_auth.exchange_google_code", lambda code, verifier: "id-token")
    monkeypatch.setattr("apps.api.app.google_auth.verify_google_id_token", lambda token, nonce: GoogleIdentity(subject="wrong-subject", email="bob@example.com"))
    client = TestClient(app)
    start = client.get(f"/auth/google/start?invitation_token={raw_invitation}", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    callback = client.get(f"/auth/google/callback?state={state}&code=provider-code", follow_redirects=False)
    assert "invitation_email_mismatch" in callback.headers["location"]
    database.refresh(invitation)
    assert invitation.status is OrganizationInvitationStatus.PENDING
    assert database.query(User).count() == 1
