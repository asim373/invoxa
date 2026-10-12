import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    AIFinding,
    AIFindingCategory,
    AIFindingSeverity,
    AIFindingStatus,
    Document,
    DocumentStatus,
    InvoiceExtraction,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationStatus,
    User,
    UserRole,
)
from apps.api.app.settings import settings


@pytest.fixture
def security_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    current_user: dict[str, User | None] = {"value": None}

    def database_override():
        yield session

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = lambda: current_user["value"]
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    try:
        yield session, current_user, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def seed_tenant(session: Session, label: str, role: OrganizationMemberRole):
    user = User(
        id=uuid.uuid4(),
        email=f"{label}@example.com",
        password_hash="test-hash",
        role=UserRole.ADMIN,
    )
    organization = Organization(id=uuid.uuid4(), name=label, slug=f"{label}-{uuid.uuid4().hex}")
    membership = OrganizationMember(
        organization=organization,
        user=user,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    session.add_all([user, organization, membership])
    session.flush()
    return user, organization


def seed_document(session: Session, owner: User, organization: Organization, name: str) -> Document:
    document = Document(
        id=uuid.uuid4(),
        owner=owner,
        created_by_user=owner,
        organization=organization,
        original_filename=name,
        mime_type="application/pdf",
        file_size=10,
        checksum=uuid.uuid4().hex + uuid.uuid4().hex,
        storage_location=name,
        status=DocumentStatus.COMPLETED,
    )
    session.add(document)
    session.flush()
    return document


def test_document_export_and_ai_idor_denied_across_organizations(security_context) -> None:
    session, current_user, client = security_context
    user_a, org_a = seed_tenant(session, "a", OrganizationMemberRole.ADMIN)
    user_b, org_b = seed_tenant(session, "b", OrganizationMemberRole.ADMIN)
    document_a = seed_document(session, user_a, org_a, "a.pdf")
    document_b = seed_document(session, user_b, org_b, "b.pdf")
    finding_b = AIFinding(
        document=document_b,
        category=AIFindingCategory.AMOUNT,
        severity=AIFindingSeverity.HIGH,
        status=AIFindingStatus.OPEN,
        signature="b-finding",
        title="B finding",
        explanation="B only",
        evidence={},
        affected_fields=[],
    )
    session.add(finding_b)
    session.commit()
    current_user["value"] = user_a
    headers = {"X-Organization-ID": str(org_a.id)}

    assert client.get("/documents", headers=headers).status_code == 200
    assert client.get(f"/documents/{document_b.id}", headers=headers).status_code == 404
    assert client.get(f"/documents/{document_b.id}/file", headers=headers).status_code == 404
    assert client.post(f"/documents/{document_b.id}/reprocess", headers=headers).status_code == 404
    assert client.delete(f"/documents/{document_b.id}", headers=headers).status_code == 404
    assert client.post(f"/documents/{document_b.id}/finalize", headers=headers).status_code == 404
    assert client.post(f"/documents/{document_b.id}/reopen", headers=headers).status_code == 404
    assert client.get(f"/documents/{document_b.id}/exports/csv", headers=headers).status_code == 404
    assert (
        client.get(f"/documents/{document_b.id}/exports/json", headers=headers).status_code == 404
    )
    assert (
        client.get(f"/documents/{document_b.id}/exports/xlsx", headers=headers).status_code == 404
    )
    for export_format in ("csv", "xlsx", "json"):
        assert client.post(
            f"/exports/{export_format}",
            json={"document_ids": [str(document_a.id), str(document_b.id)]},
            headers=headers,
        ).status_code == 404
    assert client.get(f"/documents/{document_b.id}/ai-analysis", headers=headers).status_code == 404
    assert (
        client.post(f"/documents/{document_b.id}/ai-analysis", headers=headers).status_code == 404
    )
    assert client.get("/ai-analysis/findings", headers=headers).json()["total"] == 0
    assert client.patch(
        f"/ai-analysis/findings/{finding_b.id}", json={"status": "resolved"}, headers=headers
    ).status_code == 404
    assert client.get("/analytics/summary", headers=headers).status_code == 200
    assert client.get("/reports/financial", headers=headers).status_code == 200
    assert client.get("/reports/ai-analysis", headers=headers).status_code == 200
    assert client.get("/reports/processing-quality", headers=headers).status_code == 200
    assert (
        client.get(
            "/analytics/summary", headers={"X-Organization-ID": str(org_b.id)}
        ).status_code
        == 403
    )


def test_same_organization_member_uses_org_boundary_not_owner(security_context) -> None:
    session, current_user, client = security_context
    user_a, organization = seed_tenant(session, "owner", OrganizationMemberRole.OWNER)
    user_a2 = User(
        id=uuid.uuid4(), email="member@example.com", password_hash="hash", role=UserRole.VIEWER
    )
    session.add(
        OrganizationMember(
            organization=organization,
            user=user_a2,
            role=OrganizationMemberRole.VIEWER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    document = seed_document(session, user_a, organization, "shared.pdf")
    session.commit()
    current_user["value"] = user_a2
    headers = {"X-Organization-ID": str(organization.id)}

    assert document.owner_id != user_a2.id
    assert client.get("/documents", headers=headers).json()["total"] == 1
    assert client.get(f"/documents/{document.id}", headers=headers).status_code == 200
    assert client.get(f"/documents/{document.id}/exports/json", headers=headers).status_code == 200
    assert client.get("/analytics/summary", headers=headers).status_code == 200


@pytest.mark.parametrize(
    ("role", "can_review"),
    [
        (OrganizationMemberRole.OWNER, True),
        (OrganizationMemberRole.ADMIN, True),
        (OrganizationMemberRole.REVIEWER, True),
        (OrganizationMemberRole.VIEWER, False),
    ],
)
def test_membership_role_controls_finding_mutation_but_not_read_export(
    security_context, role: OrganizationMemberRole, can_review: bool
) -> None:
    session, current_user, client = security_context
    user, organization = seed_tenant(session, role.value, role)
    document = seed_document(session, user, organization, f"{role.value}.pdf")
    finding = AIFinding(
        document=document,
        category=AIFindingCategory.AMOUNT,
        severity=AIFindingSeverity.LOW,
        status=AIFindingStatus.OPEN,
        signature=f"{role.value}-finding",
        title="Finding",
        explanation="Test finding",
        evidence={},
        affected_fields=[],
    )
    session.add(finding)
    session.commit()
    current_user["value"] = user
    headers = {"X-Organization-ID": str(organization.id)}

    assert client.get(f"/documents/{document.id}", headers=headers).status_code == 200
    assert client.get(f"/documents/{document.id}/exports/json", headers=headers).status_code == 200
    response = client.patch(
        f"/ai-analysis/findings/{finding.id}",
        json={"status": "resolved"},
        headers=headers,
    )
    assert response.status_code == (200 if can_review else 403)


@pytest.mark.parametrize(
    "status", [OrganizationMemberStatus.PENDING, OrganizationMemberStatus.INACTIVE]
)
def test_non_active_membership_cannot_select_organization(security_context, status) -> None:
    session, current_user, client = security_context
    user, organization = seed_tenant(session, "blocked", OrganizationMemberRole.ADMIN)
    membership = session.query(OrganizationMember).filter_by(user_id=user.id).one()
    membership.status = status
    session.commit()
    current_user["value"] = user

    response = client.get("/documents", headers={"X-Organization-ID": str(organization.id)})
    assert response.status_code == 403


def test_inactive_organization_cannot_provide_tenant_access(security_context) -> None:
    session, current_user, client = security_context
    user, organization = seed_tenant(session, "inactive-org", OrganizationMemberRole.ADMIN)
    organization.status = OrganizationStatus.INACTIVE
    session.commit()
    current_user["value"] = user

    response = client.get("/documents", headers={"X-Organization-ID": str(organization.id)})
    assert response.status_code == 403


def test_analytics_aggregate_excludes_other_organization_values(security_context) -> None:
    session, current_user, client = security_context
    user_a, org_a = seed_tenant(session, "aggregate-a", OrganizationMemberRole.ADMIN)
    user_b, org_b = seed_tenant(session, "aggregate-b", OrganizationMemberRole.ADMIN)
    document_a = seed_document(session, user_a, org_a, "a-total.pdf")
    document_b = seed_document(session, user_b, org_b, "b-total.pdf")
    session.add_all(
        [
            InvoiceExtraction(
                document=document_a,
                vendor_name="A-ONLY-VENDOR",
                currency="USD",
                total=111,
            ),
            InvoiceExtraction(
                document=document_b,
                vendor_name="B-ONLY-VENDOR",
                currency="USD",
                total=999,
            ),
        ]
    )
    session.commit()
    current_user["value"] = user_a
    headers = {"X-Organization-ID": str(org_a.id)}

    analytics = client.get("/analytics/summary", headers=headers).json()
    assert analytics["kpis"]["total_documents"] == 1
    assert analytics["kpis"]["currency_totals"][0]["total"] == "111.00"
    assert "B-ONLY-VENDOR" not in str(analytics)


def test_legacy_fallback_cannot_cross_into_organization_owned_document(security_context) -> None:
    session, current_user, client = security_context
    owner = User(id=uuid.uuid4(), email="legacy-owner@example.com", password_hash="hash")
    other = User(id=uuid.uuid4(), email="legacy-other@example.com", password_hash="hash")
    legacy = Document(
        owner=owner,
        original_filename="legacy.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="c" * 64,
        storage_location="legacy.pdf",
        status=DocumentStatus.COMPLETED,
    )
    organization = Organization(name="owned", slug=f"owned-{uuid.uuid4().hex}")
    owned = Document(
        owner=owner,
        organization=organization,
        original_filename="owned.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum="d" * 64,
        storage_location="owned.pdf",
        status=DocumentStatus.COMPLETED,
    )
    session.add_all([owner, other, legacy, organization, owned])
    session.commit()

    current_user["value"] = owner
    assert client.get("/documents").json()["total"] == 1
    assert client.get(f"/documents/{owned.id}").status_code == 404
    assert client.get(
        f"/documents/{owned.id}", headers={"X-Organization-ID": str(organization.id)}
    ).status_code == 403

    current_user["value"] = other
    assert client.get("/documents").json()["total"] == 0
