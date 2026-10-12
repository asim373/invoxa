from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import (
    Document,
    Organization,
    OrganizationMember,
    OrganizationMemberStatus,
    OrganizationMonthlyUsage,
    User,
)
from apps.api.app.plans import resolve_organization_entitlements
from apps.api.app.tenant import TenantContext, TenantPermission, require_tenant_permission

router = APIRouter(prefix="/organizations", tags=["usage"])


class PlanUsageResponse(BaseModel):
    code: str
    name: str


class ProcessingUsageResponse(BaseModel):
    period: str
    used_pages: int
    limit_pages: int
    remaining_pages: int


class SeatUsageResponse(BaseModel):
    used: int
    limit: int
    remaining: int


class StorageUsageResponse(BaseModel):
    used_bytes: int
    limit_bytes: int
    remaining_bytes: int


class OrganizationUsageResponse(BaseModel):
    plan: PlanUsageResponse
    processing: ProcessingUsageResponse
    seats: SeatUsageResponse
    storage: StorageUsageResponse


def _remaining(limit: int, used: int) -> int:
    return max(limit - used, 0)


def build_organization_usage(
    database: Session, organization: Organization
) -> OrganizationUsageResponse:
    entitlements = resolve_organization_entitlements(organization, database)
    if (
        entitlements.monthly_processing_limit is None
        or entitlements.active_seat_limit is None
        or entitlements.storage_bytes_limit is None
    ):
        raise HTTPException(status_code=500, detail="Organization plan requires configured quotas.")
    usage_month = datetime.now(UTC).date().replace(day=1)
    monthly = database.scalar(
        select(OrganizationMonthlyUsage).where(
            OrganizationMonthlyUsage.organization_id == organization.id,
            OrganizationMonthlyUsage.usage_month == usage_month,
        )
    )
    processing_used = 0
    if monthly is not None:
        processing_used = (
            monthly.successful_processing_pages
            if monthly.successful_processing_pages > 0
            else monthly.successful_processing_count
        )
    active_seats = (
        database.scalar(
            select(func.count(OrganizationMember.id)).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            )
        )
        or 0
    )
    storage_used = (
        database.scalar(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.organization_id == organization.id
            )
        )
        or 0
    )
    return OrganizationUsageResponse(
        plan=PlanUsageResponse(code=entitlements.code, name=entitlements.display_name),
        processing=ProcessingUsageResponse(
            period=usage_month.strftime("%Y-%m"),
            used_pages=processing_used,
            limit_pages=entitlements.monthly_processing_limit,
            remaining_pages=_remaining(entitlements.monthly_processing_limit, processing_used),
        ),
        seats=SeatUsageResponse(
            used=active_seats,
            limit=entitlements.active_seat_limit,
            remaining=_remaining(entitlements.active_seat_limit, active_seats),
        ),
        storage=StorageUsageResponse(
            used_bytes=storage_used,
            limit_bytes=entitlements.storage_bytes_limit,
            remaining_bytes=_remaining(entitlements.storage_bytes_limit, storage_used),
        ),
    )


@router.get("/usage", response_model=OrganizationUsageResponse)
def get_organization_usage(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    _: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> OrganizationUsageResponse:
    if tenant.organization is None:
        raise HTTPException(
            status_code=403, detail="You are not an active member of this organization."
        )
    return build_organization_usage(database, tenant.organization)
