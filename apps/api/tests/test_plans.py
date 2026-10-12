import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import Organization, OrganizationEnterpriseQuota
from apps.api.app.plans import (
    CAPABILITY_ADVANCED_ANOMALY_DETECTION,
    CAPABILITY_API_ACCESS,
    CAPABILITY_BASIC_ANOMALY_DETECTION,
    CAPABILITY_CUSTOM_INTEGRATIONS,
    CAPABILITY_CUSTOM_VOLUME_QUOTAS,
    CAPABILITY_FINANCIAL_VALIDATION,
    CAPABILITY_OCR_INVOICE_EXTRACTION,
    CAPABILITY_SAML_SSO,
    CAPABILITY_WEBHOOKS,
    DEFAULT_PLAN_CODE,
    ENTERPRISE_MIN_ACTIVE_SEAT_LIMIT,
    ENTERPRISE_MIN_MONTHLY_PROCESSING_LIMIT,
    ENTERPRISE_MIN_STORAGE_BYTES_LIMIT,
    ENTERPRISE_PLAN_CODE,
    PROFESSIONAL_ACTIVE_SEAT_LIMIT,
    PROFESSIONAL_MONTHLY_PROCESSING_LIMIT,
    PROFESSIONAL_PLAN_CODE,
    PROFESSIONAL_STORAGE_BYTES_LIMIT,
    STANDARD_ACTIVE_SEAT_LIMIT,
    STANDARD_MONTHLY_PROCESSING_LIMIT,
    STANDARD_STORAGE_BYTES_LIMIT,
    processing_priority_for_organization,
    resolve_organization_entitlements,
    resolve_plan_entitlements,
)


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


def test_organization_uses_standard_plan_and_authoritative_entitlements(database: Session) -> None:
    organization = Organization(name="Standard", slug=f"standard-{uuid.uuid4().hex}")
    database.add(organization)
    database.commit()
    assert organization.plan_code == DEFAULT_PLAN_CODE
    entitlements = resolve_organization_entitlements(organization)
    assert entitlements.code == "standard"
    assert entitlements.display_name == "Standard"
    assert entitlements.monthly_processing_limit == STANDARD_MONTHLY_PROCESSING_LIMIT
    assert entitlements.active_seat_limit == STANDARD_ACTIVE_SEAT_LIMIT
    assert entitlements.storage_bytes_limit == STANDARD_STORAGE_BYTES_LIMIT


def test_stale_starter_code_fails_safely() -> None:
    with pytest.raises(ValueError, match="Unknown organization plan"):
        resolve_plan_entitlements("starter")


def test_professional_inherits_standard_and_adds_commercial_capabilities() -> None:
    entitlements = resolve_plan_entitlements(PROFESSIONAL_PLAN_CODE)
    assert entitlements.display_name == "Professional"
    assert entitlements.monthly_processing_limit == PROFESSIONAL_MONTHLY_PROCESSING_LIMIT
    assert entitlements.active_seat_limit == PROFESSIONAL_ACTIVE_SEAT_LIMIT
    assert entitlements.storage_bytes_limit == PROFESSIONAL_STORAGE_BYTES_LIMIT
    assert entitlements.includes(CAPABILITY_OCR_INVOICE_EXTRACTION)
    assert entitlements.includes(CAPABILITY_BASIC_ANOMALY_DETECTION)
    assert entitlements.includes(CAPABILITY_ADVANCED_ANOMALY_DETECTION)
    assert entitlements.includes(CAPABILITY_API_ACCESS)
    assert entitlements.includes(CAPABILITY_WEBHOOKS)


def test_enterprise_inherits_capabilities_and_requires_custom_quotas() -> None:
    entitlements = resolve_plan_entitlements(ENTERPRISE_PLAN_CODE)
    assert entitlements.custom_quotas
    assert entitlements.monthly_processing_limit is None
    assert entitlements.active_seat_limit is None
    assert entitlements.storage_bytes_limit is None
    assert entitlements.includes(CAPABILITY_FINANCIAL_VALIDATION)
    assert entitlements.includes(CAPABILITY_ADVANCED_ANOMALY_DETECTION)
    assert entitlements.includes(CAPABILITY_CUSTOM_INTEGRATIONS)
    assert entitlements.includes(CAPABILITY_SAML_SSO)
    assert entitlements.includes(CAPABILITY_CUSTOM_VOLUME_QUOTAS)


def test_enterprise_minimum_custom_quotas_resolve_from_organization(database: Session) -> None:
    organization = Organization(
        name="Enterprise", slug=f"enterprise-{uuid.uuid4().hex}", plan_code=ENTERPRISE_PLAN_CODE
    )
    database.add(organization)
    database.flush()
    database.add(
        OrganizationEnterpriseQuota(
            organization_id=organization.id,
            monthly_processing_limit=ENTERPRISE_MIN_MONTHLY_PROCESSING_LIMIT,
            active_seat_limit=ENTERPRISE_MIN_ACTIVE_SEAT_LIMIT,
            storage_bytes_limit=ENTERPRISE_MIN_STORAGE_BYTES_LIMIT,
        )
    )
    database.commit()
    entitlements = resolve_organization_entitlements(organization, database)
    assert entitlements.monthly_processing_limit == 5_001
    assert entitlements.active_seat_limit == 6
    assert entitlements.storage_bytes_limit == 11_000_000_000


def test_enterprise_missing_custom_quotas_fail_safely(database: Session) -> None:
    organization = Organization(
        name="Enterprise",
        slug=f"enterprise-missing-{uuid.uuid4().hex}",
        plan_code=ENTERPRISE_PLAN_CODE,
    )
    database.add(organization)
    database.commit()
    with pytest.raises(ValueError, match="requires configured quotas"):
        resolve_organization_entitlements(organization, database)


def test_enterprise_quota_configuration_is_tenant_scoped(database: Session) -> None:
    first = Organization(
        name="Enterprise A", slug=f"enterprise-a-{uuid.uuid4().hex}", plan_code=ENTERPRISE_PLAN_CODE
    )
    second = Organization(
        name="Enterprise B", slug=f"enterprise-b-{uuid.uuid4().hex}", plan_code=ENTERPRISE_PLAN_CODE
    )
    database.add_all([first, second])
    database.flush()
    database.add(
        OrganizationEnterpriseQuota(
            organization_id=first.id,
            monthly_processing_limit=6_000,
            active_seat_limit=8,
            storage_bytes_limit=12_000_000_000,
        )
    )
    database.commit()
    with pytest.raises(ValueError, match="requires configured quotas"):
        resolve_organization_entitlements(second, database)


def test_unknown_plan_fails_safely() -> None:
    with pytest.raises(ValueError, match="Unknown organization plan"):
        resolve_plan_entitlements("client-supplied-untrusted-plan")


@pytest.mark.parametrize(
    ("plan_code", "expected"),
    [
        (DEFAULT_PLAN_CODE, "normal"),
        (PROFESSIONAL_PLAN_CODE, "priority"),
        (ENTERPRISE_PLAN_CODE, "priority"),
    ],
)
def test_processing_priority_is_derived_from_plan_capability(
    plan_code: str, expected: str
) -> None:
    organization = Organization(
        name="Priority", slug=f"priority-{uuid.uuid4().hex}", plan_code=plan_code
    )
    assert processing_priority_for_organization(organization) == expected
