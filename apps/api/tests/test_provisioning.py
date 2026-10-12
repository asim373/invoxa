import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app import auth
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.plans import DEFAULT_PLAN_CODE


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


def test_company_registration_provisions_active_owner(database: Session) -> None:
    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "owner@example.com",
            "password": "secure-password-123",
            "company_name": " Acme Logistics ",
        },
    )

    assert response.status_code == 201
    body = response.json()
    organization = database.scalar(select(Organization))
    membership = database.scalar(select(OrganizationMember))
    assert organization is not None
    assert membership is not None
    assert body["organization_id"] == str(organization.id)
    assert body["organization_name"] == "Acme Logistics"
    assert body["membership_role"] == "owner"
    assert organization.slug == "acme-logistics"
    assert membership.role is OrganizationMemberRole.OWNER
    assert membership.status is OrganizationMemberStatus.ACTIVE
    assert organization.plan_code == DEFAULT_PLAN_CODE


def test_duplicate_email_has_no_provisioning_side_effect(database: Session) -> None:
    client = TestClient(app)
    payload = {
        "email": "same@example.com",
        "password": "secure-password-123",
        "company_name": "First Company",
    }
    assert client.post("/auth/register", json=payload).status_code == 201
    duplicate = client.post(
        "/auth/register",
        json={**payload, "company_name": "Second Company"},
    )

    assert duplicate.status_code == 409
    assert database.scalar(select(User).where(User.email == "same@example.com")) is not None
    assert database.query(Organization).count() == 1
    assert database.query(OrganizationMember).count() == 1


def test_company_slug_collision_gets_unique_suffix(database: Session) -> None:
    database.add(Organization(name="Existing", slug="acme-logistics"))
    database.commit()

    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "second@example.com",
            "password": "secure-password-123",
            "company_name": "Acme Logistics",
        },
    )

    assert response.status_code == 201
    created = database.scalar(
        select(Organization).where(Organization.slug == "acme-logistics-2")
    )
    assert created is not None


def test_provisioning_failure_rolls_back_user_and_workspace(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_provisioning(*args, **kwargs):
        raise ValueError("provisioning failure")

    monkeypatch.setattr(auth, "provision_company", fail_provisioning)
    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "rollback@example.com",
            "password": "secure-password-123",
            "company_name": "Rollback Company",
        },
    )

    assert response.status_code == 422
    assert database.query(User).count() == 0
    assert database.query(Organization).count() == 0
    assert database.query(OrganizationMember).count() == 0


def test_registration_cannot_choose_owner_fields_or_existing_organization(
    database: Session,
) -> None:
    existing = Organization(name="Private", slug="private")
    database.add(existing)
    database.commit()

    response = TestClient(app).post(
        "/auth/register",
        json={
            "email": "new@example.com",
            "password": "secure-password-123",
            "company_name": "New Company",
            "organization_id": str(existing.id),
            "role": "admin",
            "membership_status": "inactive",
        },
    )

    assert response.status_code == 201
    membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == uuid.UUID(response.json()["id"])
        )
    )
    assert membership is not None
    assert membership.organization_id != existing.id
    assert membership.role is OrganizationMemberRole.OWNER
    assert membership.status is OrganizationMemberStatus.ACTIVE


def test_new_owner_can_use_provisioned_tenant(database: Session) -> None:
    client = TestClient(app)
    registration = client.post(
        "/auth/register",
        json={
            "email": "usable@example.com",
            "password": "secure-password-123",
            "company_name": "Usable Workspace",
        },
    )
    organization_id = registration.json()["organization_id"]
    organization = database.get(Organization, uuid.UUID(organization_id))
    assert organization is not None
    assert organization.plan_code == DEFAULT_PLAN_CODE
    token = client.post(
        "/auth/login",
        json={"email": "usable@example.com", "password": "secure-password-123"},
    ).json()["access_token"]

    response = client.get(
        "/documents",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Organization-ID": organization_id,
        },
    )

    assert response.status_code == 200
    assert response.json()["items"] == []
