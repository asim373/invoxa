from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.models import (
    Document,
    Organization,
    OrganizationMonthlyUsage,
    ProcessingOperation,
    ProcessingOperationStatus,
)


def account_successful_processing(
    database: Session,
    document: Document,
    operation_id,
) -> bool:
    """Mark one operation complete and increment its organization's month once."""
    operation = database.scalar(
        select(ProcessingOperation)
        .where(
            ProcessingOperation.id == operation_id,
            ProcessingOperation.document_id == document.id,
            ProcessingOperation.organization_id == document.organization_id,
        )
        .with_for_update()
    )
    if operation is None:
        raise ValueError("Processing operation does not match the document tenant.")
    if operation.status is ProcessingOperationStatus.COMPLETED:
        return False
    if document.organization_id is None:
        operation.status = ProcessingOperationStatus.COMPLETED
        operation.completed_at = datetime.now(UTC)
        return False
    now = datetime.now(UTC)
    database.scalar(
        select(Organization).where(Organization.id == document.organization_id).with_for_update()
    )
    month = now.date().replace(day=1)
    usage = database.scalar(
        select(OrganizationMonthlyUsage)
        .where(
            OrganizationMonthlyUsage.organization_id == document.organization_id,
            OrganizationMonthlyUsage.usage_month == month,
        )
        .with_for_update()
    )
    if usage is None:
        usage = OrganizationMonthlyUsage(
            organization_id=document.organization_id,
            usage_month=month,
            successful_processing_count=0,
        )
        try:
            with database.begin_nested():
                database.add(usage)
                database.flush()
        except IntegrityError:
            usage = database.scalar(
                select(OrganizationMonthlyUsage)
                .where(
                    OrganizationMonthlyUsage.organization_id == document.organization_id,
                    OrganizationMonthlyUsage.usage_month == month,
                )
                .with_for_update()
            )
            if usage is None:
                raise
    usage.successful_processing_count += 1
    usage.successful_processing_pages += operation.page_count
    operation.status = ProcessingOperationStatus.COMPLETED
    operation.completed_at = now
    return True


def mark_processing_failed(database: Session, operation_id, error_details: str) -> None:
    operation = database.get(ProcessingOperation, operation_id)
    if operation is not None and operation.status is not ProcessingOperationStatus.COMPLETED:
        operation.status = ProcessingOperationStatus.FAILED
        operation.failed_at = datetime.now(UTC)
        operation.error_details = error_details
