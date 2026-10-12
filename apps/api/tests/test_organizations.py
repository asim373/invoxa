import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import (
    Document,
    DocumentStatus,
    InvoiceExtraction,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationStatus,
    User,
    UserRole,
)


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


def make_user(email: str) -> User:
    return User(email=email, password_hash="test-password-hash", role=UserRole.REVIEWER)


def make_organization(slug: str) -> Organization:
    return Organization(name=slug.title(), slug=slug)


def test_organization_persists_with_explicit_status(database: Session) -> None:
    organization = make_organization("acme")
    database.add(organization)
    database.commit()
    database.refresh(organization)

    assert organization.id is not None
    assert organization.status is OrganizationStatus.ACTIVE
    assert organization.slug == "acme"


def test_organization_slug_is_unique(database: Session) -> None:
    database.add_all([make_organization("acme"), make_organization("acme")])

    with pytest.raises(IntegrityError):
        database.commit()


def test_user_can_belong_to_multiple_organizations(database: Session) -> None:
    user = make_user("multi@example.com")
    first = make_organization("first")
    second = make_organization("second")
    database.add_all([user, first, second])
    database.flush()
    database.add_all(
        [
            OrganizationMember(
                organization=first,
                user=user,
                role=OrganizationMemberRole.OWNER,
                status=OrganizationMemberStatus.ACTIVE,
            ),
            OrganizationMember(
                organization=second,
                user=user,
                role=OrganizationMemberRole.VIEWER,
                status=OrganizationMemberStatus.INACTIVE,
            ),
        ]
    )
    database.commit()

    assert len(user.organization_memberships) == 2
    assert {membership.organization.slug for membership in user.organization_memberships} == {
        "first",
        "second",
    }


def test_organization_can_contain_multiple_users_and_duplicate_membership_is_rejected(
    database: Session,
) -> None:
    organization = make_organization("team")
    first = make_user("first@example.com")
    second = make_user("second@example.com")
    database.add_all([organization, first, second])
    database.flush()
    database.add_all(
        [
            OrganizationMember(organization=organization, user=first),
            OrganizationMember(organization=organization, user=second),
        ]
    )
    database.commit()
    assert len(organization.memberships) == 2

    database.add(OrganizationMember(organization=organization, user=first))
    with pytest.raises(IntegrityError):
        database.commit()


@pytest.mark.parametrize("role", list(OrganizationMemberRole))
def test_membership_roles_persist(database: Session, role: OrganizationMemberRole) -> None:
    organization = make_organization(f"role-{role.value}")
    user = make_user(f"{role.value}@example.com")
    membership = OrganizationMember(organization=organization, user=user, role=role)
    database.add(membership)
    database.commit()
    database.refresh(membership)

    assert membership.role is role


def test_membership_status_lifecycle_persists(database: Session) -> None:
    organization = make_organization("lifecycle")
    user = make_user("lifecycle@example.com")
    membership = OrganizationMember(
        organization=organization,
        user=user,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add(membership)
    database.commit()

    membership.status = OrganizationMemberStatus.INACTIVE
    membership.deactivated_at = membership.updated_at
    database.commit()
    database.refresh(membership)

    assert membership.status is OrganizationMemberStatus.INACTIVE
    assert membership.deactivated_at is not None


def test_existing_user_role_and_document_ownership_model_remain_available(
    database: Session,
) -> None:
    user = make_user("legacy@example.com")
    database.add(user)
    database.commit()
    database.refresh(user)

    assert user.role is UserRole.REVIEWER
    assert hasattr(user, "documents")
    assert uuid.UUID(str(user.id)) == user.id


def test_legacy_workspace_bridge_preserves_owner_and_derived_data(database: Session) -> None:
    owner = make_user("legacy@example.com")
    organization = make_organization(f"imported-{uuid.uuid4().hex}")
    database.add_all([owner, organization])
    database.flush()
    membership = OrganizationMember(
        organization=organization,
        user=owner,
        role=OrganizationMemberRole.OWNER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    document = Document(
        owner=owner,
        organization=organization,
        created_by_user=owner,
        original_filename="legacy.pdf",
        mime_type="application/pdf",
        file_size=100,
        checksum="a" * 64,
        storage_location="legacy.pdf",
        status=DocumentStatus.COMPLETED,
    )
    extraction = InvoiceExtraction(document=document, invoice_number="INV-1")
    database.add_all([membership, document, extraction])
    database.commit()
    document_id = document.id
    owner_id = document.owner_id
    storage_location = document.storage_location

    database.refresh(document)
    assert document.id == document_id
    assert document.owner_id == owner_id == owner.id
    assert document.created_by_user_id == owner.id
    assert document.organization_id == organization.id
    assert document.storage_location == storage_location
    assert document.invoice_extraction is not None
    assert document.invoice_extraction.invoice_number == "INV-1"


def test_unresolved_null_owner_document_remains_unassigned(database: Session) -> None:
    document = Document(
        original_filename="demo.pdf",
        mime_type="application/pdf",
        file_size=100,
        checksum="b" * 64,
        storage_location="demo.pdf",
        status=DocumentStatus.COMPLETED,
    )
    database.add(document)
    database.commit()
    database.refresh(document)

    assert document.owner_id is None
    assert document.organization_id is None
    assert document.created_by_user_id is None
