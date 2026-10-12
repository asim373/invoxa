from dataclasses import dataclass, replace
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.models import Organization, OrganizationEnterpriseQuota

DEFAULT_PLAN_CODE = "standard"
PROFESSIONAL_PLAN_CODE = "professional"
ENTERPRISE_PLAN_CODE = "enterprise"
STANDARD_MONTHLY_PROCESSING_LIMIT = 1_000
STANDARD_ACTIVE_SEAT_LIMIT = 1
STANDARD_STORAGE_BYTES_LIMIT = 2_000_000_000
PROFESSIONAL_MONTHLY_PROCESSING_LIMIT = 5_000
PROFESSIONAL_ACTIVE_SEAT_LIMIT = 5
PROFESSIONAL_STORAGE_BYTES_LIMIT = 10_000_000_000
ENTERPRISE_MIN_MONTHLY_PROCESSING_LIMIT = 5_001
ENTERPRISE_MIN_ACTIVE_SEAT_LIMIT = 6
ENTERPRISE_MIN_STORAGE_BYTES_LIMIT = 11_000_000_000

STANDARD_MONTHLY_PRICE: Final[int] = 99
STANDARD_ANNUAL_PRICE: Final[int] = 990
PROFESSIONAL_MONTHLY_PRICE: Final[int] = 249
PROFESSIONAL_ANNUAL_PRICE: Final[int] = 2_490
ENTERPRISE_MONTHLY_STARTING_FLOOR: Final[int] = 799
ENTERPRISE_ANNUAL_STARTING_FLOOR: Final[int] = 7_990

CAPABILITY_OCR_INVOICE_EXTRACTION = "ocr_invoice_extraction"
CAPABILITY_HEADER_LINE_ITEM_EXTRACTION = "header_line_item_extraction"
CAPABILITY_CSV_JSON_XLSX_EXPORT = "csv_json_xlsx_export"
CAPABILITY_FINANCIAL_VALIDATION = "financial_validation"
CAPABILITY_BASIC_ANOMALY_DETECTION = "basic_anomaly_detection"
CAPABILITY_NORMAL_EMAIL_SUPPORT = "normal_email_support"
CAPABILITY_ADVANCED_ANOMALY_DETECTION = "advanced_anomaly_detection"
CAPABILITY_API_ACCESS = "api_access"
CAPABILITY_WEBHOOKS = "webhooks"
CAPABILITY_PRIORITY_PROCESSING = "priority_processing"
CAPABILITY_PRIORITY_EMAIL_SUPPORT = "priority_email_support"
CAPABILITY_TEAM_COLLABORATION = "team_collaboration_role_based_access"
CAPABILITY_DEDICATED_PRIORITY_SUPPORT = "dedicated_priority_support_sla"
CAPABILITY_CUSTOM_INTEGRATIONS = "custom_integrations"
CAPABILITY_SAML_SSO = "saml_sso"
CAPABILITY_SCIM = "scim"
CAPABILITY_ENTERPRISE_SECURITY_CONTROLS = "enterprise_security_controls"
CAPABILITY_PRIVATE_CLOUD = "private_cloud_on_prem"
CAPABILITY_CUSTOM_VOLUME_QUOTAS = "custom_volume_quotas"

STANDARD_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        CAPABILITY_OCR_INVOICE_EXTRACTION,
        CAPABILITY_HEADER_LINE_ITEM_EXTRACTION,
        CAPABILITY_CSV_JSON_XLSX_EXPORT,
        CAPABILITY_FINANCIAL_VALIDATION,
        CAPABILITY_BASIC_ANOMALY_DETECTION,
        CAPABILITY_NORMAL_EMAIL_SUPPORT,
    }
)
PROFESSIONAL_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        CAPABILITY_ADVANCED_ANOMALY_DETECTION,
        CAPABILITY_API_ACCESS,
        CAPABILITY_WEBHOOKS,
        CAPABILITY_PRIORITY_PROCESSING,
        CAPABILITY_PRIORITY_EMAIL_SUPPORT,
        CAPABILITY_TEAM_COLLABORATION,
    }
)
ENTERPRISE_CAPABILITIES: Final[frozenset[str]] = frozenset(
    {
        CAPABILITY_DEDICATED_PRIORITY_SUPPORT,
        CAPABILITY_CUSTOM_INTEGRATIONS,
        CAPABILITY_SAML_SSO,
        CAPABILITY_SCIM,
        CAPABILITY_ENTERPRISE_SECURITY_CONTROLS,
        CAPABILITY_PRIVATE_CLOUD,
        CAPABILITY_CUSTOM_VOLUME_QUOTAS,
    }
)


@dataclass(frozen=True)
class PlanEntitlements:
    code: str
    display_name: str
    monthly_processing_limit: int | None
    active_seat_limit: int | None
    storage_bytes_limit: int | None
    monthly_price: int | None
    annual_price: int | None
    seat_description: str
    capabilities: frozenset[str]
    custom_quotas: bool = False

    def includes(self, capability: str) -> bool:
        return capability in self.capabilities


_STANDARD_ENTITLEMENTS = PlanEntitlements(
    code=DEFAULT_PLAN_CODE,
    display_name="Standard",
    monthly_processing_limit=STANDARD_MONTHLY_PROCESSING_LIMIT,
    active_seat_limit=STANDARD_ACTIVE_SEAT_LIMIT,
    storage_bytes_limit=STANDARD_STORAGE_BYTES_LIMIT,
    monthly_price=STANDARD_MONTHLY_PRICE,
    annual_price=STANDARD_ANNUAL_PRICE,
    seat_description="1 active seat",
    capabilities=STANDARD_CAPABILITIES,
)

_PROFESSIONAL_ENTITLEMENTS = PlanEntitlements(
    code=PROFESSIONAL_PLAN_CODE,
    display_name="Professional",
    monthly_processing_limit=PROFESSIONAL_MONTHLY_PROCESSING_LIMIT,
    active_seat_limit=PROFESSIONAL_ACTIVE_SEAT_LIMIT,
    storage_bytes_limit=PROFESSIONAL_STORAGE_BYTES_LIMIT,
    monthly_price=PROFESSIONAL_MONTHLY_PRICE,
    annual_price=PROFESSIONAL_ANNUAL_PRICE,
    seat_description="Team Seats 5 + Team Collaboration & Role-Based Access",
    capabilities=STANDARD_CAPABILITIES | PROFESSIONAL_CAPABILITIES,
)

_ENTERPRISE_ENTITLEMENTS = PlanEntitlements(
    code=ENTERPRISE_PLAN_CODE,
    display_name="Enterprise",
    monthly_processing_limit=None,
    active_seat_limit=None,
    storage_bytes_limit=None,
    monthly_price=None,
    annual_price=None,
    seat_description="Custom team seats",
    capabilities=STANDARD_CAPABILITIES | PROFESSIONAL_CAPABILITIES | ENTERPRISE_CAPABILITIES,
    custom_quotas=True,
)

_PLAN_ENTITLEMENTS = {
    DEFAULT_PLAN_CODE: _STANDARD_ENTITLEMENTS,
    PROFESSIONAL_PLAN_CODE: _PROFESSIONAL_ENTITLEMENTS,
    ENTERPRISE_PLAN_CODE: _ENTERPRISE_ENTITLEMENTS,
}


def resolve_plan_entitlements(plan_code: str) -> PlanEntitlements:
    try:
        return _PLAN_ENTITLEMENTS[plan_code]
    except KeyError:
        raise ValueError(f"Unknown organization plan: {plan_code}") from None


def resolve_organization_entitlements(
    organization: Organization, database: Session | None = None
) -> PlanEntitlements:
    entitlements = resolve_plan_entitlements(organization.plan_code)
    if organization.plan_code != ENTERPRISE_PLAN_CODE or database is None:
        return entitlements
    quota = database.scalar(
        select(OrganizationEnterpriseQuota).where(
            OrganizationEnterpriseQuota.organization_id == organization.id
        )
    )
    if quota is None:
        raise ValueError("Enterprise organization requires configured quotas")
    if (
        quota.monthly_processing_limit < ENTERPRISE_MIN_MONTHLY_PROCESSING_LIMIT
        or quota.active_seat_limit < ENTERPRISE_MIN_ACTIVE_SEAT_LIMIT
        or quota.storage_bytes_limit < ENTERPRISE_MIN_STORAGE_BYTES_LIMIT
    ):
        raise ValueError("Enterprise organization has invalid quotas")
    return replace(
        entitlements,
        monthly_processing_limit=quota.monthly_processing_limit,
        active_seat_limit=quota.active_seat_limit,
        storage_bytes_limit=quota.storage_bytes_limit,
    )


def processing_priority_for_organization(organization: Organization) -> str:
    """Return the durable queue class granted by the authoritative plan."""
    entitlements = resolve_plan_entitlements(organization.plan_code)
    return "priority" if entitlements.includes(CAPABILITY_PRIORITY_PROCESSING) else "normal"
