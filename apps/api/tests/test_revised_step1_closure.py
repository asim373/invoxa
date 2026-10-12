import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import create_access_token, hash_password
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Document,
    DocumentStatus,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.plans import PROFESSIONAL_PLAN_CODE


@pytest.fixture
def closure_context():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    app.dependency_overrides[get_db] = lambda: session
    try:
        yield session, TestClient(app)
    finally:
        app.dependency_overrides.clear()
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def make_workspace(session: Session):
    owner = User(
        email=f"owner-{uuid.uuid4()}@example.com", password_hash=hash_password("owner-password")
    )
    organization = Organization(
        name="Closure Workspace",
        slug=f"closure-{uuid.uuid4().hex}",
        plan_code=PROFESSIONAL_PLAN_CODE,
    )
    session.add(
        OrganizationMember(
            organization=organization,
            user=owner,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    session.commit()
    return owner, organization


def auth_headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(user.id)}",
        "X-Organization-ID": str(organization.id),
    }


def add_member(
    session: Session, organization: Organization, role: OrganizationMemberRole, label: str
) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4()}@example.com", password_hash=hash_password("member-password")
    )
    session.add(
        OrganizationMember(
            organization=organization, user=user, role=role, status=OrganizationMemberStatus.ACTIVE
        )
    )
    session.commit()
    return user


def add_document(session: Session, owner: User, organization: Organization) -> Document:
    document = Document(
        owner=owner,
        created_by_user=owner,
        organization=organization,
        original_filename="invoice.pdf",
        mime_type="application/pdf",
        file_size=10,
        checksum=uuid.uuid4().hex * 2,
        storage_location="invoice.pdf",
        status=DocumentStatus.COMPLETED,
    )
    session.add(document)
    session.commit()
    return document


def test_revised_invitation_hierarchy_and_no_owner(closure_context):
    session, client = closure_context
    owner, organization = make_workspace(session)
    admin = add_member(session, organization, OrganizationMemberRole.ADMIN, "admin")
    member = add_member(session, organization, OrganizationMemberRole.MEMBER, "member")
    owner_headers = auth_headers(owner, organization)
    admin_headers = auth_headers(admin, organization)
    member_headers = auth_headers(member, organization)

    assert (
        client.post(
            "/team/invitations", headers=owner_headers, json={"email": "a@x.com", "role": "admin"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/team/invitations", headers=owner_headers, json={"email": "m@x.com", "role": "member"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/team/invitations", headers=admin_headers, json={"email": "m2@x.com", "role": "member"}
        ).status_code
        == 201
    )
    assert (
        client.post(
            "/team/invitations", headers=admin_headers, json={"email": "a2@x.com", "role": "admin"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/team/invitations", headers=admin_headers, json={"email": "o@x.com", "role": "owner"}
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/team/invitations", headers=member_headers, json={"email": "x@x.com", "role": "member"}
        ).status_code
        == 403
    )


def test_password_reset_authority_and_token_security(closure_context):
    session, client = closure_context
    owner, organization = make_workspace(session)
    admin = add_member(session, organization, OrganizationMemberRole.ADMIN, "admin")
    member = add_member(session, organization, OrganizationMemberRole.MEMBER, "member")
    other_admin = add_member(session, organization, OrganizationMemberRole.ADMIN, "other-admin")
    member_record = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == member.id)
    )
    admin_record = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == admin.id)
    )
    owner_record = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == owner.id)
    )
    assert member_record and admin_record and owner_record

    owner_reset = client.post(
        f"/team/members/{admin_record.id}/password-reset", headers=auth_headers(owner, organization)
    )
    assert owner_reset.status_code == 200
    temporary_password = owner_reset.json()["temporary_password"]
    assert temporary_password
    session.refresh(admin)
    assert admin.force_password_change is True
    assert temporary_password not in admin.password_hash
    admin_login = client.post(
        "/auth/login", json={"email": admin.email, "password": temporary_password}
    )
    assert admin_login.status_code == 200
    forced_headers = {
        "Authorization": f"Bearer {admin_login.json()['access_token']}",
        "X-Organization-ID": str(organization.id),
    }
    assert client.get("/team/members", headers=forced_headers).status_code == 403
    changed = client.post(
        "/auth/change-password",
        headers=forced_headers,
        json={"new_password": "Admin-Private-Password-123!"},
    )
    assert changed.status_code == 200
    admin_login = client.post(
        "/auth/login", json={"email": admin.email, "password": "Admin-Private-Password-123!"}
    )
    assert admin_login.status_code == 200
    admin_headers = {
        "Authorization": f"Bearer {admin_login.json()['access_token']}",
        "X-Organization-ID": str(organization.id),
    }
    assert (
        client.post(
            f"/team/members/{member_record.id}/password-reset",
            headers=admin_headers,
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/team/members/{owner_record.id}/password-reset",
            headers=admin_headers,
        ).status_code
        == 403
    )
    other_admin_record = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == other_admin.id)
    )
    assert other_admin_record
    assert (
        client.post(
            f"/team/members/{other_admin_record.id}/password-reset",
            headers=admin_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/team/members/{member_record.id}/password-reset",
            headers={
                "Authorization": f"Bearer {create_access_token(member.id, member.auth_version)}",
                "X-Organization-ID": str(organization.id),
            },
        ).status_code
        == 403
    )
    session.refresh(admin)
    assert admin.force_password_change is False
    assert temporary_password not in admin.password_hash


def test_finalize_reopen_lifecycle_and_member_lock(closure_context):
    session, client = closure_context
    owner, organization = make_workspace(session)
    admin = add_member(session, organization, OrganizationMemberRole.ADMIN, "admin")
    member = add_member(session, organization, OrganizationMemberRole.MEMBER, "member")
    document = add_document(session, owner, organization)
    member_headers = auth_headers(member, organization)
    owner_headers = auth_headers(owner, organization)
    admin_headers = auth_headers(admin, organization)

    finalized = client.post(f"/documents/{document.id}/finalize", headers=member_headers)
    assert finalized.status_code == 200
    session.refresh(document)
    assert document.is_finalized is True and document.finalized_at is not None
    assert document.finalized_by_user_id == member.id
    assert client.delete(f"/documents/{document.id}", headers=member_headers).status_code == 403
    assert (
        client.post(f"/documents/{document.id}/reopen", headers=member_headers).status_code == 403
    )
    assert client.post(f"/documents/{document.id}/reopen", headers=admin_headers).status_code == 200
    session.refresh(document)
    assert document.is_finalized is False and document.reopened_at is not None
    assert document.reopened_by_user_id == admin.id
    assert (
        client.post(f"/documents/{document.id}/finalize", headers=owner_headers).status_code == 200
    )
    session.refresh(document)
    assert document.finalized_by_user_id == owner.id


def test_historical_viewer_remains_read_only(closure_context):
    session, client = closure_context
    owner, organization = make_workspace(session)
    viewer = add_member(session, organization, OrganizationMemberRole.VIEWER, "viewer")
    document = add_document(session, owner, organization)
    session.commit()
    headers = auth_headers(viewer, organization)
    assert client.get(f"/documents/{document.id}", headers=headers).status_code == 200
    assert client.post(f"/documents/{document.id}/finalize", headers=headers).status_code == 403
    assert client.post(f"/documents/{document.id}/reprocess", headers=headers).status_code == 403
