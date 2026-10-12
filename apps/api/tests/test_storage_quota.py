import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import Document, DocumentStatus, Organization
from apps.api.app.storage_quota import (
    STORAGE_LIMIT_REACHED_MESSAGE,
    require_storage_capacity,
)


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def organization(database: Session, name: str = "Storage QA") -> Organization:
    item = Organization(name=name, slug=f"storage-{uuid.uuid4().hex}")
    database.add(item)
    database.flush()
    return item


def document(database: Session, item: Organization, size: int) -> Document:
    stored = Document(
        organization_id=item.id,
        original_filename="source.pdf",
        mime_type="application/pdf",
        file_size=size,
        checksum="a" * 64,
        storage_location=f"{uuid.uuid4()}.pdf",
        status=DocumentStatus.FAILED,
    )
    database.add(stored)
    database.flush()
    return stored


def test_storage_quota_allows_below_and_exact_limit(database: Session) -> None:
    item = organization(database)
    from apps.api.app.plans import STANDARD_STORAGE_BYTES_LIMIT

    document(database, item, STANDARD_STORAGE_BYTES_LIMIT - 10)
    require_storage_capacity(database, item, 10)


def test_storage_quota_rejects_only_when_usage_would_exceed_limit(database: Session) -> None:
    item = organization(database)
    from apps.api.app.plans import STANDARD_STORAGE_BYTES_LIMIT

    document(database, item, STANDARD_STORAGE_BYTES_LIMIT)
    with pytest.raises(HTTPException) as error:
        require_storage_capacity(database, item, 1)
    assert error.value.status_code == 409
    assert error.value.detail == STORAGE_LIMIT_REACHED_MESSAGE


def test_storage_quota_is_organization_scoped_and_ignores_legacy_rows(
    database: Session,
) -> None:
    first = organization(database, "First")
    second = organization(database, "Second")
    from apps.api.app.plans import STANDARD_STORAGE_BYTES_LIMIT

    document(database, first, STANDARD_STORAGE_BYTES_LIMIT)
    document(database, second, 1)
    database.add(
        Document(
            organization_id=None,
            original_filename="legacy.pdf",
            mime_type="application/pdf",
            file_size=STANDARD_STORAGE_BYTES_LIMIT,
            checksum="b" * 64,
            storage_location="legacy.pdf",
            status=DocumentStatus.FAILED,
        )
    )
    database.flush()
    require_storage_capacity(database, second, STANDARD_STORAGE_BYTES_LIMIT - 1)

    usage = database.scalar(
        select(func.coalesce(func.sum(Document.file_size), 0)).where(
            Document.organization_id == second.id
        )
    )
    assert usage == 1


def test_storage_quota_rejects_unknown_authoritative_plan(database: Session) -> None:
    item = organization(database)
    item.plan_code = "unknown"
    with pytest.raises(HTTPException) as error:
        require_storage_capacity(database, item, 1)
    assert error.value.status_code == 500
