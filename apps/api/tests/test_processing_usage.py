import uuid
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import (
    Document,
    DocumentStatus,
    Organization,
    OrganizationMonthlyUsage,
    ProcessingOperation,
    ProcessingOperationStatus,
)
from apps.api.app.processing_quota import (
    PROCESSING_LIMIT_REACHED_MESSAGE,
    require_processing_capacity,
)
from apps.api.app.usage import account_successful_processing, mark_processing_failed


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


def make_document(database: Session, organization: Organization | None) -> Document:
    document = Document(
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=uuid.uuid4().hex,
        storage_location=f"{uuid.uuid4()}.pdf",
        organization=organization,
        status=DocumentStatus.PROCESSING,
    )
    database.add(document)
    database.flush()
    return document


def make_operation(database: Session, document: Document) -> ProcessingOperation:
    operation = ProcessingOperation(
        document_id=document.id, organization_id=document.organization_id
    )
    database.add(operation)
    database.flush()
    return operation


def test_success_is_counted_once_and_reprocessing_is_a_new_unit(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    document = make_document(database, organization)
    first = make_operation(database, document)
    document.status = DocumentStatus.COMPLETED

    assert account_successful_processing(database, document, first.id) is True
    database.commit()
    assert account_successful_processing(database, document, first.id) is False
    usage = database.scalar(select(OrganizationMonthlyUsage))
    assert usage is not None and usage.successful_processing_count == 1

    document.status = DocumentStatus.QUEUED
    document.status = DocumentStatus.PROCESSING
    second = make_operation(database, document)
    document.status = DocumentStatus.COMPLETED
    assert account_successful_processing(database, document, second.id) is True
    database.commit()
    usage = database.scalar(select(OrganizationMonthlyUsage))
    assert usage is not None
    assert usage.successful_processing_count == 2


def test_usage_period_is_the_current_utc_completion_month(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    document = make_document(database, organization)
    operation = make_operation(database, document)
    document.status = DocumentStatus.COMPLETED

    assert account_successful_processing(database, document, operation.id) is True
    database.commit()
    usage = database.scalar(select(OrganizationMonthlyUsage))
    assert usage is not None
    assert usage.usage_month == datetime.now(UTC).date().replace(day=1)


def test_failed_operation_and_legacy_document_do_not_create_usage(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    document = make_document(database, organization)
    operation = make_operation(database, document)
    mark_processing_failed(database, operation.id, "extract failed")
    database.commit()
    assert database.scalar(select(OrganizationMonthlyUsage)) is None
    failed_operation = database.get(ProcessingOperation, operation.id)
    assert failed_operation is not None
    assert failed_operation.status is ProcessingOperationStatus.FAILED

    legacy_document = make_document(database, None)
    legacy_operation = make_operation(database, legacy_document)
    legacy_document.status = DocumentStatus.COMPLETED
    assert account_successful_processing(database, legacy_document, legacy_operation.id) is False
    database.commit()
    assert database.scalar(select(OrganizationMonthlyUsage)) is None


def test_operation_tenant_mismatch_cannot_be_accounted(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    other = Organization(name="Other", slug=f"other-{uuid.uuid4().hex}")
    database.add_all([organization, other])
    database.flush()
    document = make_document(database, organization)
    operation = ProcessingOperation(document_id=document.id, organization_id=other.id)
    database.add(operation)
    database.flush()
    document.status = DocumentStatus.COMPLETED

    with pytest.raises(ValueError, match="does not match"):
        account_successful_processing(database, document, operation.id)
    assert database.scalar(select(OrganizationMonthlyUsage)) is None


def test_deleting_document_does_not_refund_historical_usage(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    document = make_document(database, organization)
    operation = make_operation(database, document)
    document.status = DocumentStatus.COMPLETED
    assert account_successful_processing(database, document, operation.id) is True
    database.commit()
    database.delete(document)
    database.commit()
    usage = database.scalar(select(OrganizationMonthlyUsage))
    assert usage is not None and usage.successful_processing_count == 1


def test_processing_admission_rejects_at_authoritative_monthly_limit(database: Session) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    database.add(
        OrganizationMonthlyUsage(
            organization_id=organization.id,
            usage_month=datetime.now(UTC).date().replace(day=1),
            successful_processing_pages=1_000,
        )
    )
    database.commit()

    with pytest.raises(HTTPException, match=PROCESSING_LIMIT_REACHED_MESSAGE) as error:
        require_processing_capacity(database, organization)
    assert error.value.status_code == 409


def test_processing_admission_allows_exact_standard_limit_and_rejects_overage(
    database: Session,
) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    database.add(
        OrganizationMonthlyUsage(
            organization_id=organization.id,
            usage_month=datetime.now(UTC).date().replace(day=1),
            successful_processing_pages=999,
        )
    )
    database.commit()

    require_processing_capacity(database, organization, incoming_pages=1)
    with pytest.raises(HTTPException, match=PROCESSING_LIMIT_REACHED_MESSAGE) as error:
        require_processing_capacity(database, organization, incoming_pages=2)
    assert error.value.status_code == 409


def test_processing_admission_counts_active_operations_as_temporary_reservations(
    database: Session,
) -> None:
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}")
    database.add(organization)
    database.flush()
    document = make_document(database, organization)
    database.add_all([make_operation(database, document) for _ in range(1_000)])
    database.commit()

    with pytest.raises(HTTPException, match=PROCESSING_LIMIT_REACHED_MESSAGE):
        require_processing_capacity(database, organization)
