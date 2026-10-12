import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import create_access_token, issue_access_token
from apps.api.app.database import Base, get_db
from apps.api.app.email import get_invitation_email_sender
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationSamlConfiguration,
    OrganizationSecurityAuditEvent,
    User,
    UserSession,
)
from apps.api.app.settings import settings


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        app.dependency_overrides[get_db] = lambda: session
        yield session
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def seed(
    db: Session,
    *,
    role: OrganizationMemberRole = OrganizationMemberRole.OWNER,
    plan: str = "enterprise",
):
    user = User(email=f"owner-{uuid.uuid4().hex}@example.com", password_hash="unused")
    org = Organization(name="Security QA", slug=f"security-{uuid.uuid4().hex}", plan_code=plan)
    membership = OrganizationMember(
        organization=org,
        user=user,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    db.add_all([user, org, membership])
    db.commit()
    return user, org, membership


def auth_headers(user: User, org: Organization | None = None):
    headers = {"Authorization": f"Bearer {create_access_token(user.id, user.auth_version or 0)}"}
    if org is not None:
        headers["X-Organization-ID"] = str(org.id)
    return headers


def test_mandatory_sso_requires_enabled_saml_and_one_time_owner_recovery(db, monkeypatch):
    from apps.api.app import saml_sso

    monkeypatch.setattr(saml_sso, "_sp_config", lambda organization, configuration: None)
    owner, org, _ = seed(db)
    client = TestClient(app)
    headers = auth_headers(owner, org)

    rejected = client.put("/organizations/security/sso", headers=headers, json={"enabled": True})
    assert rejected.status_code == 409
    db.add(
        OrganizationSamlConfiguration(
            organization_id=org.id,
            enabled=True,
            idp_entity_id="https://idp.example/entity",
            idp_sso_url="https://idp.example/sso",
            signing_certificates=["validated-by-existing-saml-route"],
            attribute_mappings={},
        )
    )
    db.commit()
    enabled = client.put("/organizations/security/sso", headers=headers, json={"enabled": True})
    assert enabled.status_code == 200
    recovery_code = enabled.json()["recovery_code"]
    assert recovery_code and len(recovery_code) >= 32
    assert client.get("/team/members", headers=headers).status_code == 403

    wrong = client.post(
        "/organizations/security/sso/recover",
        headers=headers,
        json={"code": "x" * 40},
    )
    assert wrong.status_code == 403
    recovered = client.post(
        "/organizations/security/sso/recover",
        headers=headers,
        json={"code": recovery_code},
    )
    assert recovered.status_code == 200
    assert recovered.json() == {"enabled": False, "recovery_code": None}
    assert (
        client.post(
            "/organizations/security/sso/recover",
            headers=headers,
            json={"code": recovery_code},
        ).status_code
        == 403
    )
    assert client.get("/team/members", headers=headers).status_code == 200
    audit_response = client.get("/organizations/security/audit-events", headers=headers)
    assert audit_response.status_code == 200
    assert any(event["action"] == "sso_policy_recovered" for event in audit_response.json())
    event = db.scalar(
        select(OrganizationSecurityAuditEvent).where(
            OrganizationSecurityAuditEvent.organization_id == org.id,
            OrganizationSecurityAuditEvent.action == "sso_policy_recovered",
        )
    )
    assert event is not None and recovery_code not in str(event.context)


def test_sso_scoped_session_only_authorizes_its_organization(db):
    owner, org, _ = seed(db)
    other_org = Organization(name="Other", slug=f"other-{uuid.uuid4().hex}", plan_code="enterprise")
    other_org.sso_enforced = True
    other_org.sso_recovery_code_hash = "a" * 64
    other_membership = OrganizationMember(
        organization=other_org,
        user=owner,
        role=OrganizationMemberRole.OWNER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    other_org.saml_configuration = OrganizationSamlConfiguration(
        enabled=True,
        idp_entity_id="https://other-idp.example/entity",
        idp_sso_url="https://other-idp.example/sso",
        signing_certificates=["other validated"],
        attribute_mappings={},
    )
    org.sso_enforced = True
    org.sso_recovery_code_hash = "b" * 64
    org.saml_configuration = OrganizationSamlConfiguration(
        enabled=True,
        idp_entity_id="https://idp.example/entity",
        idp_sso_url="https://idp.example/sso",
        signing_certificates=["already validated"],
        attribute_mappings={},
    )
    db.add_all([other_org, other_membership])
    db.commit()
    session_id = uuid.uuid4()
    expiry = datetime.now(UTC) + timedelta(minutes=30)
    db.add(
        UserSession(
            id=session_id,
            user_id=owner.id,
            auth_version=owner.auth_version,
            sso_organization_id=org.id,
            expires_at=expiry,
        )
    )
    db.commit()
    token = create_access_token(
        owner.id, owner.auth_version, session_id=session_id, sso_organization_id=org.id
    )
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-ID": str(org.id)}
    assert client.get("/team/members", headers=headers).status_code == 200
    assert client.delete("/organizations/saml", headers=headers).status_code == 409
    headers["X-Organization-ID"] = str(other_org.id)
    assert client.get("/team/members", headers=headers).status_code == 403


def test_saml_issued_session_is_persisted_and_workspace_scoped(db):
    owner, org, _ = seed(db)
    org.sso_enforced = True
    org.sso_recovery_code_hash = "c" * 64
    org.saml_configuration = OrganizationSamlConfiguration(
        enabled=True,
        idp_entity_id="https://idp.example/entity",
        idp_sso_url="https://idp.example/sso",
        signing_certificates=["valid in the existing SAML path"],
        attribute_mappings={},
    )
    db.commit()
    token = issue_access_token(db, owner, sso_organization_id=org.id)
    db.commit()
    response = TestClient(app).get(
        "/team/members",
        headers={"Authorization": f"Bearer {token}", "X-Organization-ID": str(org.id)},
    )
    assert response.status_code == 200
    session = db.scalar(select(UserSession).where(UserSession.user_id == owner.id))
    assert session is not None and session.sso_organization_id == org.id


def test_logout_revokes_persisted_current_session(db):
    user, _org, _membership = seed(db)
    client = TestClient(app)
    headers = auth_headers(user)
    assert client.get("/auth/sessions", headers=headers).status_code == 200
    response = client.get("/auth/sessions", headers=headers)
    assert response.json() and any(row["current"] for row in response.json())
    assert client.post("/auth/logout", headers=headers).status_code == 204
    assert client.get("/auth/sessions", headers=headers).status_code == 401
    assert (
        db.scalar(select(UserSession).where(UserSession.user_id == user.id)).revoked_at is not None
    )


def test_individual_session_revocation_rejects_that_token(db):
    user, _org, _membership = seed(db)
    client = TestClient(app)
    headers = auth_headers(user)
    sessions = client.get("/auth/sessions", headers=headers).json()
    session_id = sessions[0]["id"]
    assert client.delete(f"/auth/sessions/{session_id}", headers=headers).status_code == 204
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_revoke_other_sessions_preserves_only_current_session(db):
    user, _org, _membership = seed(db)
    client = TestClient(app)
    current_headers = auth_headers(user)
    other_headers = auth_headers(user)
    assert client.get("/auth/me", headers=current_headers).status_code == 200
    assert client.get("/auth/me", headers=other_headers).status_code == 200
    assert client.post("/auth/sessions/revoke-others", headers=current_headers).status_code == 204
    assert client.get("/auth/me", headers=current_headers).status_code == 200
    assert client.get("/auth/me", headers=other_headers).status_code == 401


def test_production_invitation_sends_secret_without_returning_it(db, monkeypatch):
    owner, org, _membership = seed(db, plan="professional")
    owner_token = issue_access_token(db, owner)
    db.commit()
    monkeypatch.setattr(settings, "app_environment", "production")

    class Sender:
        called = False

        def send_invitation(self, recipient, invitation_url, organization_name):
            self.called = True
            assert recipient == "invitee@example.com"
            assert "/invite/accept?token=" in invitation_url

    sender = Sender()
    app.dependency_overrides[get_invitation_email_sender] = lambda: sender
    try:
        response = TestClient(app).post(
            "/team/invitations",
            headers={"Authorization": f"Bearer {owner_token}", "X-Organization-ID": str(org.id)},
            json={"email": "invitee@example.com", "role": "member"},
        )
    finally:
        app.dependency_overrides.pop(get_invitation_email_sender, None)
        monkeypatch.setattr(settings, "app_environment", "development")
    assert response.status_code == 201
    assert sender.called
    assert response.json()["invitation_token"] is None
    assert response.json()["delivery_status"] == "sent"


def test_production_admin_password_reset_delivers_link_not_temporary_password(db, monkeypatch):
    from apps.api.app.team import get_password_reset_email_sender as team_reset_sender

    owner, org, _ = seed(db, plan="professional")
    target = User(email="reset-target@example.com", password_hash="old-hash")
    member = OrganizationMember(
        organization=org,
        user=target,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    db.add_all([target, member])
    db.commit()
    owner_token = issue_access_token(db, owner)
    db.commit()
    monkeypatch.setattr(settings, "app_environment", "production")

    class Sender:
        url = None

        def send_password_reset(self, recipient, reset_url):
            assert recipient == target.email
            self.url = reset_url

    sender = Sender()
    app.dependency_overrides[team_reset_sender] = lambda: sender
    try:
        response = TestClient(app).post(
            f"/team/members/{member.id}/password-reset",
            headers={"Authorization": f"Bearer {owner_token}", "X-Organization-ID": str(org.id)},
        )
    finally:
        app.dependency_overrides.pop(team_reset_sender, None)
        monkeypatch.setattr(settings, "app_environment", "development")
    assert response.status_code == 200
    assert response.json()["temporary_password"] is None
    assert sender.url and "/reset-password?token=" in sender.url
    assert target.password_hash != "old-hash"
    assert target.force_password_change is True
    assert target.auth_version == 1
