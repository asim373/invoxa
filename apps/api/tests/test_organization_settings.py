import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import create_access_token
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationSecurityAuditEvent,
    User,
)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
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


def seed(database: Session, role: OrganizationMemberRole):
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", password_hash="unused")
    organization = Organization(name="Initial Company", slug=f"company-{uuid.uuid4().hex}")
    membership = OrganizationMember(
        organization=organization,
        user=user,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all([user, organization, membership])
    database.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token(user.id)}",
        "X-Organization-ID": str(organization.id),
    }
    return user, organization, headers


def test_owner_reads_and_updates_company_name_with_audit(database: Session) -> None:
    _, organization, headers = seed(database, OrganizationMemberRole.OWNER)
    client = TestClient(app)

    initial = client.get("/organizations/current", headers=headers)
    updated = client.patch(
        "/organizations/current", headers=headers, json={"name": "  Northwind Studio  "}
    )

    assert initial.status_code == 200
    assert initial.json() == {
        "id": str(organization.id),
        "name": "Initial Company",
        "slug": organization.slug,
    }
    assert updated.status_code == 200
    assert updated.json()["name"] == "Northwind Studio"
    assert updated.json()["slug"] == organization.slug
    event = database.scalar(
        select(OrganizationSecurityAuditEvent).where(
            OrganizationSecurityAuditEvent.organization_id == organization.id
        )
    )
    assert event is not None
    assert event.action == "organization_profile_updated"
    assert event.actor_user_id is not None
    assert event.context == {}


def test_admin_can_edit_but_member_cannot(database: Session) -> None:
    _, organization, headers = seed(database, OrganizationMemberRole.ADMIN)
    client = TestClient(app)
    assert client.patch(
        "/organizations/current", headers=headers, json={"name": "Admin Edit"}
    ).status_code == 200

    member = User(id=uuid.uuid4(), email="member@example.com", password_hash="unused")
    database.add(
        OrganizationMember(
            organization_id=organization.id,
            user=member,
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    database.commit()
    member_headers = {
        "Authorization": f"Bearer {create_access_token(member.id)}",
        "X-Organization-ID": str(organization.id),
    }
    denied = client.patch(
        "/organizations/current", headers=member_headers, json={"name": "Unauthorized"}
    )
    assert denied.status_code == 403
    assert organization.name == "Admin Edit"


def test_foreign_tenant_cannot_read_or_update_company_profile(database: Session) -> None:
    _, organization, owner_headers = seed(database, OrganizationMemberRole.OWNER)
    foreign_user = User(id=uuid.uuid4(), email="foreign@example.com", password_hash="unused")
    foreign_org = Organization(name="Private Co", slug=f"private-{uuid.uuid4().hex}")
    database.add_all(
        [
            foreign_user,
            foreign_org,
            OrganizationMember(
                organization=foreign_org,
                user=foreign_user,
                role=OrganizationMemberRole.OWNER,
                status=OrganizationMemberStatus.ACTIVE,
            ),
        ]
    )
    database.commit()
    foreign_headers = {
        "Authorization": owner_headers["Authorization"],
        "X-Organization-ID": str(foreign_org.id),
    }
    client = TestClient(app)
    assert client.get("/organizations/current", headers=foreign_headers).status_code == 403
    assert client.patch(
        "/organizations/current", headers=foreign_headers, json={"name": "Changed"}
    ).status_code == 403
    assert foreign_org.name == "Private Co"
    assert organization.name == "Initial Company"


@pytest.mark.parametrize("name", ["", "   ", "x" * 256])
def test_invalid_company_name_is_rejected(database: Session, name: str) -> None:
    _, _, headers = seed(database, OrganizationMemberRole.OWNER)
    response = TestClient(app).patch("/organizations/current", headers=headers, json={"name": name})
    assert response.status_code == 422
