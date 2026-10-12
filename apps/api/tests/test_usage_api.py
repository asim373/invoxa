import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import create_access_token, get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Document,
    DocumentStatus,
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationMonthlyUsage,
    User,
)
from apps.api.app.plans import (
    DEFAULT_PLAN_CODE,
    STANDARD_ACTIVE_SEAT_LIMIT,
    STANDARD_MONTHLY_PROCESSING_LIMIT,
    STANDARD_STORAGE_BYTES_LIMIT,
)
from apps.api.app.usage_api import build_organization_usage


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
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        Base.metadata.drop_all(engine)
        engine.dispose()


def workspace(
    database: Session, email: str = "usage-owner@example.com"
) -> tuple[User, Organization]:
    owner = User(email=email, password_hash="unused")
    item = Organization(name="Usage Workspace", slug=f"usage-{uuid.uuid4().hex}")
    database.add(
        OrganizationMember(
            organization=item,
            user=owner,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    database.commit()
    return owner, item


def headers(owner: User, item: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(owner.id)}",
        "X-Organization-ID": str(item.id),
    }


def test_usage_snapshot_uses_authoritative_plan_and_current_month(database: Session) -> None:
    owner, item = workspace(database)
    database.add(
        OrganizationMonthlyUsage(
            organization_id=item.id,
            usage_month=datetime.now(UTC).date().replace(day=1),
            successful_processing_pages=12,
        )
    )
    database.add(
        OrganizationMember(
            organization_id=item.id,
            user=User(email="inactive-usage@example.com", password_hash="unused"),
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationMemberStatus.INACTIVE,
        )
    )
    database.add(
        OrganizationInvitation(
            organization_id=item.id,
            invited_email="pending-usage@example.com",
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationInvitationStatus.PENDING,
            token_hash="a" * 64,
            invited_by_user_id=owner.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    database.add(
        Document(
            organization_id=item.id,
            original_filename="completed.pdf",
            mime_type="application/pdf",
            file_size=100,
            checksum="b" * 64,
            storage_location="completed.pdf",
            status=DocumentStatus.COMPLETED,
        )
    )
    database.add(
        Document(
            organization_id=item.id,
            original_filename="failed.pdf",
            mime_type="application/pdf",
            file_size=50,
            checksum="c" * 64,
            storage_location="failed.pdf",
            status=DocumentStatus.FAILED,
        )
    )
    database.commit()

    snapshot = build_organization_usage(database, item)
    assert snapshot.plan.code == DEFAULT_PLAN_CODE
    assert snapshot.plan.name == "Standard"
    assert len(snapshot.processing.period) == 7
    assert snapshot.processing.period[4] == "-"
    assert snapshot.processing.used_pages == 12
    assert snapshot.processing.limit_pages == STANDARD_MONTHLY_PROCESSING_LIMIT
    assert snapshot.processing.remaining_pages == STANDARD_MONTHLY_PROCESSING_LIMIT - 12
    assert snapshot.seats.used == 1
    assert snapshot.seats.limit == STANDARD_ACTIVE_SEAT_LIMIT
    assert snapshot.seats.remaining == STANDARD_ACTIVE_SEAT_LIMIT - 1
    assert snapshot.storage.used_bytes == 150
    assert snapshot.storage.limit_bytes == STANDARD_STORAGE_BYTES_LIMIT


def test_usage_snapshot_missing_or_previous_month_reads_as_zero_without_mutation(
    database: Session,
) -> None:
    _, item = workspace(database)
    previous = datetime.now(UTC).date().replace(day=1) - timedelta(days=1)
    database.add(
        OrganizationMonthlyUsage(
            organization_id=item.id,
            usage_month=previous.replace(day=1),
            successful_processing_count=99,
        )
    )
    database.commit()
    before = database.query(OrganizationMonthlyUsage).count()
    snapshot = build_organization_usage(database, item)
    assert snapshot.processing.used_pages == 0
    assert database.query(OrganizationMonthlyUsage).count() == before


def test_usage_remaining_values_clamp_at_zero(database: Session) -> None:
    _, item = workspace(database)
    database.add(
        OrganizationMonthlyUsage(
            organization_id=item.id,
            usage_month=datetime.now(UTC).date().replace(day=1),
            successful_processing_pages=STANDARD_MONTHLY_PROCESSING_LIMIT + 1,
        )
    )
    for index in range(6):
        database.add(
            OrganizationMember(
                organization_id=item.id,
                user=User(email=f"seat-{index}@example.com", password_hash="unused"),
                role=OrganizationMemberRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
            )
        )
    database.add(
        Document(
            organization_id=item.id,
            original_filename="large.pdf",
            mime_type="application/pdf",
            file_size=STANDARD_STORAGE_BYTES_LIMIT + 1,
            checksum="d" * 64,
            storage_location="large.pdf",
            status=DocumentStatus.UPLOADED,
        )
    )
    database.commit()
    snapshot = build_organization_usage(database, item)
    assert snapshot.processing.remaining_pages == 0
    assert snapshot.seats.remaining == 0
    assert snapshot.storage.remaining_bytes == 0


def test_usage_snapshot_excludes_other_organizations_and_legacy_documents(
    database: Session,
) -> None:
    _, first = workspace(database, "first-usage@example.com")
    _, second = workspace(database, "second-usage@example.com")
    database.add_all(
        [
            Document(
                organization_id=first.id,
                original_filename="first.pdf",
                mime_type="application/pdf",
                file_size=10,
                checksum="e" * 64,
                storage_location="first.pdf",
            ),
            Document(
                organization_id=None,
                original_filename="legacy.pdf",
                mime_type="application/pdf",
                file_size=20,
                checksum="f" * 64,
                storage_location="legacy.pdf",
            ),
            Document(
                organization_id=second.id,
                original_filename="second.pdf",
                mime_type="application/pdf",
                file_size=30,
                checksum="0" * 64,
                storage_location="second.pdf",
            ),
        ]
    )
    database.commit()
    assert build_organization_usage(database, first).storage.used_bytes == 10


def test_usage_endpoint_requires_matching_active_tenant(database: Session) -> None:
    owner, item = workspace(database)
    _, other = workspace(database, "other-usage@example.com")
    app.dependency_overrides[get_current_user] = lambda: owner
    client = TestClient(app)
    assert (
        client.get("/organizations/usage", headers=headers(owner, item)).status_code
        == 200
    )
    assert (
        client.get("/organizations/usage", headers=headers(owner, other)).status_code
        == 403
    )
    assert (
        client.get(
            "/organizations/usage",
            headers={"Authorization": headers(owner, item)["Authorization"]},
        ).status_code
        == 403
    )


def test_usage_endpoint_rejects_unauthenticated_request(database: Session) -> None:
    _, item = workspace(database)
    client = TestClient(app)
    response = client.get("/organizations/usage")
    assert response.status_code == 401
