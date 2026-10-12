import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Document,
    DocumentStatus,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationMonthlyUsage,
    ProcessingOperation,
    User,
)
from apps.api.app.plans import STANDARD_MONTHLY_PROCESSING_LIMIT
from apps.api.app.queue import get_document_processing_queue
from apps.api.app.settings import settings

LIMIT_MESSAGE = "This workspace has reached its monthly processing page limit."


def pdf_bytes(page_count: int = 1) -> bytes:
    import io

    writer = PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID | None]] = []

    def enqueue(
        self,
        document_id: uuid.UUID,
        organization_id: uuid.UUID | None = None,
        operation_id: uuid.UUID | None = None,
    ) -> None:
        self.calls.append((document_id, organization_id, operation_id))


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


def make_context(database: Session) -> tuple[User, Organization]:
    user = User(email=f"quota-{uuid.uuid4().hex}@example.com", password_hash="not-used")
    organization = Organization(name="Quota", slug=f"quota-{uuid.uuid4().hex}")
    database.add_all([user, organization])
    database.flush()
    database.add(
        OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    database.commit()
    return user, organization


def set_at_limit(database: Session, organization: Organization) -> None:
    database.add(
        OrganizationMonthlyUsage(
            organization_id=organization.id,
            usage_month=datetime.now(UTC).date().replace(day=1),
            successful_processing_pages=STANDARD_MONTHLY_PROCESSING_LIMIT,
        )
    )
    database.commit()


def client_for(database: Session, user: User, queue: RecordingQueue):
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_document_processing_queue] = lambda: queue
    return TestClient(app)


def clear_overrides() -> None:
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_document_processing_queue, None)


def test_upload_processing_quota_admission_and_rejection(
    database: Session, tmp_path: Path, monkeypatch
) -> None:
    user, organization = make_context(database)
    queue = RecordingQueue()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    client = client_for(database, user, queue)
    headers = {"X-Organization-ID": str(organization.id)}
    try:
        accepted = client.post(
            "/documents",
            headers=headers,
            files={"file": ("allowed.pdf", pdf_bytes(), "application/pdf")},
        )
        assert accepted.status_code == 201
        assert len(queue.calls) == 1
        assert database.scalar(
            select(ProcessingOperation).where(
                ProcessingOperation.document_id == uuid.UUID(accepted.json()["id"])
            )
        ) is not None

        set_at_limit(database, organization)
        queued_before = len(queue.calls)
        operations_before = database.scalar(select(func.count(ProcessingOperation.id)))
        rejected = client.post(
            "/documents",
            headers=headers,
            files={"file": ("blocked.pdf", pdf_bytes(), "application/pdf")},
        )
    finally:
        clear_overrides()

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == LIMIT_MESSAGE
    assert len(queue.calls) == queued_before
    assert database.scalar(select(func.count(ProcessingOperation.id))) == operations_before
    assert not (tmp_path / "blocked.pdf").exists()


def test_reprocess_processing_quota_admission_and_rejection(
    database: Session, tmp_path: Path, monkeypatch
) -> None:
    user, organization = make_context(database)
    queue = RecordingQueue()
    stored_file = tmp_path / "reprocess.pdf"
    stored_file.write_bytes(pdf_bytes())
    document = Document(
        original_filename="reprocess.pdf",
        mime_type="application/pdf",
        file_size=20,
        checksum=uuid.uuid4().hex,
        storage_location=stored_file.name,
        organization_id=organization.id,
        owner_id=user.id,
        created_by_user_id=user.id,
        status=DocumentStatus.FAILED,
    )
    database.add(document)
    database.commit()
    monkeypatch.setattr(settings, "storage_path", tmp_path)
    client = client_for(database, user, queue)
    headers = {"X-Organization-ID": str(organization.id)}
    try:
        accepted = client.post(f"/documents/{document.id}/reprocess", headers=headers)
        assert accepted.status_code == 200
        assert len(queue.calls) == 1

        document.status = DocumentStatus.FAILED
        database.commit()
        set_at_limit(database, organization)
        queued_before = len(queue.calls)
        operations_before = database.scalar(select(func.count(ProcessingOperation.id)))
        rejected = client.post(f"/documents/{document.id}/reprocess", headers=headers)
    finally:
        clear_overrides()

    assert rejected.status_code == 409
    assert rejected.json()["detail"] == LIMIT_MESSAGE
    assert document.status is DocumentStatus.FAILED
    assert len(queue.calls) == queued_before
    assert database.scalar(select(func.count(ProcessingOperation.id))) == operations_before
