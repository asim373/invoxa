import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.export_service import ExportDocumentsNotFoundError, _verify_owned_documents
from apps.api.app.models import Document, DocumentStatus, Organization, User


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_document(owner: User, organization: Organization, filename: str) -> Document:
    return Document(
        owner=owner,
        organization=organization,
        created_by_user=owner,
        original_filename=filename,
        mime_type="application/pdf",
        file_size=10,
        checksum=uuid.uuid4().hex + uuid.uuid4().hex,
        storage_location=filename,
        status=DocumentStatus.COMPLETED,
    )


def test_batch_export_scope_rejects_mixed_organization_documents(database: Session) -> None:
    user_a = User(email="a@example.com", password_hash="hash")
    user_b = User(email="b@example.com", password_hash="hash")
    organization_a = Organization(name="A", slug="a")
    organization_b = Organization(name="B", slug="b")
    document_a = make_document(user_a, organization_a, "a.pdf")
    document_b = make_document(user_b, organization_b, "b.pdf")
    database.add_all([user_a, user_b, organization_a, organization_b, document_a, document_b])
    database.commit()

    with pytest.raises(ExportDocumentsNotFoundError):
        _verify_owned_documents(
            database,
            user_a.id,
            (document_a.id, document_b.id),
            organization_a.id,
        )


def test_organization_export_scope_does_not_fallback_to_owner(database: Session) -> None:
    owner = User(email="owner@example.com", password_hash="hash")
    organization_a = Organization(name="A", slug="owner-a")
    organization_b = Organization(name="B", slug="owner-b")
    document = make_document(owner, organization_b, "b.pdf")
    database.add_all([owner, organization_a, organization_b, document])
    database.commit()

    with pytest.raises(ExportDocumentsNotFoundError):
        _verify_owned_documents(database, owner.id, (document.id,), organization_a.id)
