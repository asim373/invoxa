from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.models import (
    Organization,
    OrganizationMonthlyUsage,
    ProcessingOperation,
    ProcessingOperationStatus,
)
from apps.api.app.plans import resolve_organization_entitlements

PROCESSING_LIMIT_REACHED_MESSAGE = "This workspace has reached its monthly processing page limit."


def require_processing_capacity(
    database: Session, organization: Organization, incoming_pages: int = 1
) -> None:
    """Reserve admission capacity for one new processing operation."""
    if incoming_pages < 1:
        raise ValueError("Processing page count must be positive.")
    locked = database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    try:
        limit = resolve_organization_entitlements(locked, database).monthly_processing_limit
    except ValueError:
        raise HTTPException(
            status_code=500, detail="Organization plan configuration is invalid."
        ) from None

    now = datetime.now(UTC)
    month = now.date().replace(day=1)
    next_month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    usage = database.scalar(
        select(OrganizationMonthlyUsage).where(
            OrganizationMonthlyUsage.organization_id == locked.id,
            OrganizationMonthlyUsage.usage_month == month,
        )
    )
    completed = 0
    if usage is not None:
        # Pre-page-accounting rows have no reconstructable page quantity. Treat
        # each legacy successful operation conservatively as one page.
        completed = (
            usage.successful_processing_pages
            if usage.successful_processing_pages > 0
            else usage.successful_processing_count
        )
    reserved = (
        database.scalar(
            select(func.coalesce(func.sum(ProcessingOperation.page_count), 0)).where(
                ProcessingOperation.organization_id == locked.id,
                ProcessingOperation.status.in_(
                    [ProcessingOperationStatus.QUEUED, ProcessingOperationStatus.PROCESSING]
                ),
                ProcessingOperation.created_at >= month,
                ProcessingOperation.created_at < next_month,
            )
        )
        or 0
    )
    if limit is None:
        raise HTTPException(status_code=500, detail="Organization plan requires configured quotas.")
    if completed + reserved + incoming_pages > limit:
        raise HTTPException(status_code=409, detail=PROCESSING_LIMIT_REACHED_MESSAGE)
