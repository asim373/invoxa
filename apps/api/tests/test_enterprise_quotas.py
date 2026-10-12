import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import (
    Document,
    Organization,
    OrganizationEnterpriseQuota,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationMonthlyUsage,
    User,
)
from apps.api.app.plans import (
    ENTERPRISE_PLAN_CODE,
    resolve_organization_entitlements,
)
from apps.api.app.processing_quota import require_processing_capacity
from apps.api.app.seat_quota import require_active_seat
from apps.api.app.storage_quota import require_storage_capacity


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def enterprise(database: Session) -> Organization:
    organization = Organization(
        name="Enterprise", slug=f"enterprise-{uuid.uuid4().hex}", plan_code=ENTERPRISE_PLAN_CODE
    )
    database.add(organization)
    database.flush()
    database.add(
        OrganizationEnterpriseQuota(
            organization_id=organization.id,
            monthly_processing_limit=5_001,
            active_seat_limit=6,
            storage_bytes_limit=11_000_000_000,
        )
    )
    database.commit()
    return organization


def test_enterprise_processing_exact_limit_and_overage(database: Session) -> None:
    organization = enterprise(database)
    usage = OrganizationMonthlyUsage(
        organization_id=organization.id,
        usage_month=date.today().replace(day=1),
        successful_processing_pages=5_000,
    )
    database.add(usage)
    database.commit()
    require_processing_capacity(database, organization, incoming_pages=1)
    usage.successful_processing_pages = 5_001
    database.commit()
    with pytest.raises(HTTPException) as error:
        require_processing_capacity(database, organization, incoming_pages=1)
    assert error.value.status_code == 409


def test_enterprise_seat_limit_is_authoritative(database: Session) -> None:
    organization = enterprise(database)
    for _index in range(6):
        user = User(
            email=f"enterprise-seat-{uuid.uuid4().hex}@example.invalid", password_hash="x"
        )
        database.add(user)
        database.flush()
        database.add(
            OrganizationMember(
                organization_id=organization.id,
                user_id=user.id,
                role=OrganizationMemberRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
            )
        )
    database.commit()
    with pytest.raises(HTTPException) as error:
        require_active_seat(database, organization)
    assert error.value.status_code == 409


def test_enterprise_storage_exact_limit_and_overage(database: Session) -> None:
    organization = enterprise(database)
    owner = User(
        email=f"enterprise-storage-{uuid.uuid4().hex}@example.invalid", password_hash="x"
    )
    database.add(owner)
    database.flush()
    database.add(
        Document(
            original_filename="source.pdf",
            mime_type="application/pdf",
            file_size=10_999_999_999,
            checksum=uuid.uuid4().hex,
            storage_location="source.pdf",
            owner_id=owner.id,
            created_by_user_id=owner.id,
            organization_id=organization.id,
        )
    )
    database.commit()
    require_storage_capacity(database, organization, 1)
    with pytest.raises(HTTPException) as error:
        require_storage_capacity(database, organization, 2)
    assert error.value.status_code == 409


def test_enterprise_quota_resolution_does_not_fallback_to_fixed_plan_values(
    database: Session,
) -> None:
    organization = enterprise(database)
    entitlements = resolve_organization_entitlements(organization, database)
    assert entitlements.monthly_processing_limit == 5_001
    assert entitlements.active_seat_limit == 6
    assert entitlements.storage_bytes_limit == 11_000_000_000
