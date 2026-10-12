# ruff: noqa: E501

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import create_access_token
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
)
from apps.api.app.plans import DEFAULT_PLAN_CODE, PROFESSIONAL_PLAN_CODE
from apps.api.app.seat_quota import INVITATION_SEAT_LIMIT_REACHED_MESSAGE


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            app.dependency_overrides[get_db] = lambda: session
            yield session
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def seed(
    database: Session,
    role: OrganizationMemberRole = OrganizationMemberRole.OWNER,
    email: str = "owner@example.com",
):
    user = User(id=uuid.uuid4(), email=email, password_hash="unused")
    organization = Organization(
        name="Acme", slug=f"acme-{uuid.uuid4().hex}", plan_code=PROFESSIONAL_PLAN_CODE
    )
    member = OrganizationMember(
        organization=organization,
        user=user,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all([user, organization, member])
    database.commit()
    return user, organization


def headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(user.id)}",
        "X-Organization-ID": str(organization.id),
    }


def add_active_members(database: Session, organization: Organization, count: int) -> None:
    for _ in range(count):
        user = User(
            id=uuid.uuid4(), email=f"seat-{uuid.uuid4().hex}@example.com", password_hash="unused"
        )
        database.add(
            OrganizationMember(
                organization=organization,
                user=user,
                role=OrganizationMemberRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
            )
        )
    database.commit()


def create_member_invitation(
    client: TestClient, owner: User, organization: Organization, email: str
) -> dict:
    response = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": email, "role": "member"},
    )
    assert response.status_code == 201
    return response.json()


def deactivate_owner_to_free_standard_seat(
    database: Session, organization: Organization, owner: User
) -> None:
    membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == owner.id,
        )
    )
    assert membership is not None
    membership.status = OrganizationMemberStatus.INACTIVE
    database.commit()


def test_owner_creates_scoped_invitation_without_exposing_token_in_list(database: Session) -> None:
    user, organization = seed(database)
    client = TestClient(app)
    response = client.post(
        "/team/invitations",
        headers=headers(user, organization),
        json={"email": "new@example.com", "role": "reviewer"},
    )
    assert response.status_code == 201
    assert response.json()["invitation_token"]
    listed = client.get("/team/invitations", headers=headers(user, organization))
    assert listed.status_code == 200
    assert "invitation_token" not in listed.text
    invitation = database.scalar(select(OrganizationInvitation))
    assert invitation is not None
    assert invitation.token_hash != response.json()["invitation_token"]


def test_invitation_creation_blocks_at_or_above_standard_seat_limit(
    database: Session,
) -> None:
    owner, organization = seed(database)
    organization.plan_code = DEFAULT_PLAN_CODE
    database.commit()
    client = TestClient(app)
    blocked = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "standard-blocked@example.com", "role": "member"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == INVITATION_SEAT_LIMIT_REACHED_MESSAGE
    assert database.scalar(select(OrganizationInvitation)) is None

    owner, organization = seed(database, email="standard-over-limit@example.com")
    organization.plan_code = DEFAULT_PLAN_CODE
    add_active_members(database, organization, 1)
    blocked_above_limit = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "standard-over-limit-target@example.com", "role": "member"},
    )
    assert blocked_above_limit.status_code == 409
    assert (
        database.scalar(
            select(OrganizationInvitation).where(
                OrganizationInvitation.organization_id == organization.id
            )
        )
        is None
    )


def test_invitation_creation_below_legacy_limit_still_works(database: Session) -> None:
    owner, organization = seed(database, email="legacy-below-limit@example.com")
    add_active_members(database, organization, 3)
    response = TestClient(app).post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "legacy-below-limit-target@example.com", "role": "member"},
    )
    assert response.status_code == 201


def test_fresh_and_regenerated_invitation_tokens_are_immediately_valid(database: Session) -> None:
    owner, organization = seed(database)
    client = TestClient(app)
    created = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "fresh@example.com", "role": "member"},
    ).json()
    created_expires_at = datetime.fromisoformat(created["expires_at"].replace("Z", "+00:00"))
    if created_expires_at.tzinfo is None:
        created_expires_at = created_expires_at.replace(tzinfo=UTC)
    assert created_expires_at > datetime.now(UTC)
    assert (
        client.get(
            "/team/invitations/preview", params={"token": created["invitation_token"]}
        ).status_code
        == 200
    )

    regenerated = client.post(
        f"/team/invitations/{created['id']}/resend", headers=headers(owner, organization)
    ).json()
    assert regenerated["invitation_token"] != created["invitation_token"]
    regenerated_expires_at = datetime.fromisoformat(
        regenerated["expires_at"].replace("Z", "+00:00")
    )
    if regenerated_expires_at.tzinfo is None:
        regenerated_expires_at = regenerated_expires_at.replace(tzinfo=UTC)
    assert regenerated_expires_at > datetime.now(UTC)
    assert (
        client.get(
            "/team/invitations/preview", params={"token": regenerated["invitation_token"]}
        ).status_code
        == 200
    )
    assert (
        client.get(
            "/team/invitations/preview", params={"token": created["invitation_token"]}
        ).status_code
        == 400
    )


def test_existing_invitation_acceptance_enforces_active_seat_limit(database: Session) -> None:
    owner, organization = seed(database)
    target = User(email="seat-existing@example.com", password_hash="unused")
    database.add(target)
    database.commit()
    add_active_members(database, organization, 3)
    client = TestClient(app)
    invitation = create_member_invitation(client, owner, organization, target.email)
    add_active_members(database, organization, 1)

    response = client.post(
        "/team/invitations/accept",
        headers={"Authorization": f"Bearer {create_access_token(target.id)}"},
        json={"token": invitation["invitation_token"]},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "This workspace has reached its active member limit."
    database.refresh(target)
    persisted = database.scalar(select(OrganizationInvitation))
    assert persisted is not None
    assert persisted.status is OrganizationInvitationStatus.PENDING
    assert (
        database.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == target.id,
            )
        )
        is None
    )


def test_new_invitation_acceptance_succeeds_below_limit_and_rolls_back_at_limit(
    database: Session,
) -> None:
    owner, organization = seed(database)
    client = TestClient(app)
    invitation = create_member_invitation(client, owner, organization, "seat-new@example.com")
    deactivate_owner_to_free_standard_seat(database, organization, owner)
    accepted = client.post(
        "/team/invitations/accept-new",
        json={
            "token": invitation["invitation_token"],
            "email": "seat-new@example.com",
            "password": "a-strong-password-123",
        },
    )
    assert accepted.status_code == 201

    owner, organization = seed(database, email="owner-limit@example.com")
    limited = create_member_invitation(client, owner, organization, "seat-limit@example.com")
    add_active_members(database, organization, 4)
    rejected = client.post(
        "/team/invitations/accept-new",
        json={
            "token": limited["invitation_token"],
            "email": "seat-limit@example.com",
            "password": "a-strong-password-123",
        },
    )
    assert rejected.status_code == 409
    assert database.scalar(select(User).where(User.email == "seat-limit@example.com")) is None


def test_pending_and_inactive_memberships_do_not_consume_seats(database: Session) -> None:
    owner, organization = seed(database)
    inactive = User(email="inactive-seat@example.com", password_hash="unused")
    database.add(
        OrganizationMember(
            organization=organization,
            user=inactive,
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationMemberStatus.INACTIVE,
        )
    )
    database.commit()
    client = TestClient(app)
    pending = create_member_invitation(client, owner, organization, "pending-seat@example.com")
    deactivate_owner_to_free_standard_seat(database, organization, owner)
    accepted = client.post(
        "/team/invitations/accept-new",
        json={
            "token": pending["invitation_token"],
            "email": "pending-seat@example.com",
            "password": "a-strong-password-123",
        },
    )
    assert accepted.status_code == 201


def test_inactive_membership_reactivation_is_rejected_at_limit(database: Session) -> None:
    owner, organization = seed(database)
    inactive = User(email="reactivate-seat@example.com", password_hash="unused")
    database.add(
        OrganizationMember(
            organization=organization,
            user=inactive,
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationMemberStatus.INACTIVE,
        )
    )
    database.commit()
    client = TestClient(app)
    invitation = create_member_invitation(client, owner, organization, inactive.email)
    add_active_members(database, organization, 4)
    response = client.post(
        "/team/invitations/accept",
        headers={"Authorization": f"Bearer {create_access_token(inactive.id)}"},
        json={"token": invitation["invitation_token"]},
    )
    assert response.status_code == 409
    membership = database.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == inactive.id)
    )
    assert membership is not None
    assert membership.status is OrganizationMemberStatus.INACTIVE


def test_expired_invitation_is_rejected(database: Session) -> None:
    owner, organization = seed(database)
    client = TestClient(app)
    created = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "expired@example.com", "role": "member"},
    ).json()
    invitation = database.scalar(select(OrganizationInvitation))
    assert invitation is not None
    invitation.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    database.commit()
    assert (
        client.get(
            "/team/invitations/preview", params={"token": created["invitation_token"]}
        ).status_code
        == 400
    )


def test_invitation_revoke_only_allows_pending_and_preserves_lifecycle_fields(
    database: Session,
) -> None:
    owner, organization = seed(database)
    other_owner, other_organization = seed(database, email="other-owner@example.com")
    client = TestClient(app)

    pending = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "pending-revoke@example.com", "role": "member"},
    ).json()
    cross_tenant = client.post(
        f"/team/invitations/{pending['id']}/revoke",
        headers=headers(other_owner, other_organization),
    )
    assert cross_tenant.status_code == 404
    database.expire_all()
    cross_tenant_invitation = database.get(OrganizationInvitation, uuid.UUID(pending["id"]))
    assert cross_tenant_invitation is not None
    assert cross_tenant_invitation.status is OrganizationInvitationStatus.PENDING

    revoked = client.post(
        f"/team/invitations/{pending['id']}/revoke",
        headers=headers(owner, organization),
    )
    assert revoked.status_code == 200
    database.expire_all()
    revoked_invitation = database.get(OrganizationInvitation, uuid.UUID(pending["id"]))
    assert revoked_invitation is not None
    assert revoked_invitation.status is OrganizationInvitationStatus.REVOKED
    assert revoked_invitation.revoked_at is not None
    revoked_at = revoked_invitation.revoked_at

    repeated = client.post(
        f"/team/invitations/{pending['id']}/revoke",
        headers=headers(owner, organization),
    )
    assert repeated.status_code == 400
    database.expire_all()
    repeated_invitation = database.get(OrganizationInvitation, uuid.UUID(pending["id"]))
    assert repeated_invitation is not None
    assert repeated_invitation.status is OrganizationInvitationStatus.REVOKED
    assert repeated_invitation.revoked_at == revoked_at

    accepted = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "accepted-revoke@example.com", "role": "member"},
    ).json()
    deactivate_owner_to_free_standard_seat(database, organization, owner)
    accepted_response = client.post(
        "/team/invitations/accept-new",
        json={
            "token": accepted["invitation_token"],
            "email": "accepted-revoke@example.com",
            "password": "secure-password-123",
        },
    )
    assert accepted_response.status_code == 201
    database.expire_all()
    accepted_invitation = database.get(OrganizationInvitation, uuid.UUID(accepted["id"]))
    assert accepted_invitation is not None
    assert accepted_invitation.status is OrganizationInvitationStatus.ACCEPTED
    accepted_at = accepted_invitation.accepted_at
    accepted_revoked_at = accepted_invitation.revoked_at

    owner_membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.user_id == owner.id,
        )
    )
    assert owner_membership is not None
    owner_membership.status = OrganizationMemberStatus.ACTIVE
    database.commit()

    accepted_revoke = client.post(
        f"/team/invitations/{accepted['id']}/revoke",
        headers=headers(owner, organization),
    )
    assert accepted_revoke.status_code == 400
    database.expire_all()
    unchanged_accepted = database.get(OrganizationInvitation, uuid.UUID(accepted["id"]))
    assert unchanged_accepted is not None
    assert unchanged_accepted.status is OrganizationInvitationStatus.ACCEPTED
    assert unchanged_accepted.accepted_at == accepted_at
    assert unchanged_accepted.revoked_at == accepted_revoked_at


def test_team_routes_without_selected_organization_return_controlled_error(
    database: Session,
) -> None:
    user, organization = seed(database)
    client = TestClient(app)
    auth_only = {"Authorization": f"Bearer {create_access_token(user.id)}"}

    response = client.get("/team/members", headers=auth_only)

    assert response.status_code == 400
    assert "active organization" in response.json()["detail"]


def test_invitation_preview_is_server_derived_and_accept_new_returns_session(
    database: Session,
) -> None:
    owner, organization = seed(database)
    client = TestClient(app)
    invitation = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "preview@example.com", "role": "member"},
    ).json()
    original_plan = organization.plan_code
    preview = client.get(
        "/team/invitations/preview", params={"token": invitation["invitation_token"]}
    )
    assert preview.status_code == 200
    assert preview.json()["organization_name"] == organization.name
    assert preview.json()["role"] == "member"
    assert "token_hash" not in preview.text
    deactivate_owner_to_free_standard_seat(database, organization, owner)
    accepted = client.post(
        "/team/invitations/accept-new",
        json={
            "token": invitation["invitation_token"],
            "email": "preview@example.com",
            "password": "secure-password-123",
        },
    )
    assert accepted.status_code == 201
    assert accepted.json()["access_token"]
    assert accepted.json()["organization_id"] == str(organization.id)
    assert organization.plan_code == original_plan


@pytest.mark.parametrize(
    "role,expected",
    [
        (OrganizationMemberRole.ADMIN, 201),
        (OrganizationMemberRole.REVIEWER, 201),
        (OrganizationMemberRole.VIEWER, 201),
    ],
)
def test_owner_role_matrix(database: Session, role: OrganizationMemberRole, expected: int) -> None:
    user, organization = seed(database)
    response = TestClient(app).post(
        "/team/invitations",
        headers=headers(user, organization),
        json={"email": f"{role.value}@example.com", "role": role.value},
    )
    assert response.status_code == expected


def test_admin_cannot_invite_admin_or_owner(database: Session) -> None:
    user, organization = seed(database, OrganizationMemberRole.ADMIN)
    client = TestClient(app)
    for role in ("admin", "owner"):
        response = client.post(
            "/team/invitations",
            headers=headers(user, organization),
            json={"email": f"{role}@example.com", "role": role},
        )
        assert response.status_code == 403


def test_admin_cannot_demote_peer_admin_or_modify_owner(database: Session) -> None:
    admin, organization = seed(database, OrganizationMemberRole.ADMIN)
    peer_admin = User(email="peer-admin@example.com", password_hash="unused")
    owner = User(email="actual-owner@example.com", password_hash="unused")
    peer_membership = OrganizationMember(
        organization=organization,
        user=peer_admin,
        role=OrganizationMemberRole.ADMIN,
        status=OrganizationMemberStatus.ACTIVE,
    )
    owner_membership = OrganizationMember(
        organization=organization,
        user=owner,
        role=OrganizationMemberRole.OWNER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all([peer_admin, owner, peer_membership, owner_membership])
    database.commit()
    client = TestClient(app)

    demote = client.patch(
        f"/team/members/{peer_membership.id}",
        headers=headers(admin, organization),
        json={"role": "member"},
    )
    owner_change = client.patch(
        f"/team/members/{owner_membership.id}",
        headers=headers(admin, organization),
        json={"role": "member"},
    )

    assert demote.status_code == 403
    assert peer_membership.role is OrganizationMemberRole.ADMIN
    assert owner_change.status_code == 403
    assert owner_membership.role is OrganizationMemberRole.OWNER


def test_owner_can_manage_privileged_role_and_admin_can_manage_member(database: Session) -> None:
    owner, organization = seed(database)
    member_user = User(email="role-target@example.com", password_hash="unused")
    member = OrganizationMember(
        organization=organization,
        user=member_user,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    admin_user = User(email="role-admin@example.com", password_hash="unused")
    admin_member = OrganizationMember(
        organization=organization,
        user=admin_user,
        role=OrganizationMemberRole.ADMIN,
        status=OrganizationMemberStatus.ACTIVE,
    )
    admin_target_user = User(email="admin-role-target@example.com", password_hash="unused")
    admin_target = OrganizationMember(
        organization=organization,
        user=admin_target_user,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all(
        [member_user, member, admin_user, admin_member, admin_target_user, admin_target]
    )
    database.commit()
    client = TestClient(app)

    owner_result = client.patch(
        f"/team/members/{member.id}",
        headers=headers(owner, organization),
        json={"role": "admin"},
    )
    admin_result = client.patch(
        f"/team/members/{admin_target.id}",
        headers=headers(admin_user, organization),
        json={"role": "member"},
    )

    assert owner_result.status_code == 200
    assert owner_result.json()["role"] == "admin"
    assert admin_result.status_code == 200
    assert admin_result.json()["role"] == "member"


def test_role_change_foreign_tenant_is_denied(database: Session) -> None:
    admin, organization = seed(database, OrganizationMemberRole.ADMIN)
    _, foreign_org = seed(database, OrganizationMemberRole.OWNER, "foreign-owner@example.com")
    foreign_user = User(email="foreign-member@example.com", password_hash="unused")
    foreign_member = OrganizationMember(
        organization=foreign_org,
        user=foreign_user,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all([foreign_user, foreign_member])
    database.commit()

    response = TestClient(app).patch(
        f"/team/members/{foreign_member.id}",
        headers=headers(admin, organization),
        json={"role": "member"},
    )

    assert response.status_code == 403
    assert foreign_member.role is OrganizationMemberRole.MEMBER


def test_new_invitee_joins_existing_org_and_token_is_single_use(database: Session) -> None:
    owner, organization = seed(database)
    client = TestClient(app)
    invitation = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": "new@example.com", "role": "viewer"},
    ).json()
    deactivate_owner_to_free_standard_seat(database, organization, owner)
    accepted = client.post(
        "/team/invitations/accept-new",
        json={
            "token": invitation["invitation_token"],
            "email": "new@example.com",
            "password": "secure-password-123",
        },
    )
    assert accepted.status_code == 201
    assert database.query(Organization).count() == 1
    assert database.query(OrganizationMember).count() == 2
    replay = client.post(
        "/team/invitations/accept-new",
        json={
            "token": invitation["invitation_token"],
            "email": "new@example.com",
            "password": "secure-password-123",
        },
    )
    assert replay.status_code == 400


def test_authenticated_email_mismatch_is_denied(database: Session) -> None:
    owner, organization = seed(database)
    invitation = (
        TestClient(app)
        .post(
            "/team/invitations",
            headers=headers(owner, organization),
            json={"email": "new@example.com", "role": "viewer"},
        )
        .json()
    )
    other = User(id=uuid.uuid4(), email="other@example.com", password_hash="unused")
    database.add(other)
    database.commit()
    response = TestClient(app).post(
        "/team/invitations/accept",
        headers=headers(other, organization),
        json={"token": invitation["invitation_token"]},
    )
    assert response.status_code == 403


def test_team_management_rejects_foreign_member_and_invitation_ids(database: Session) -> None:
    owner_a, organization_a = seed(database, email="owner-a@example.com")
    owner_b, organization_b = seed(database, email="owner-b@example.com")
    foreign_user = User(id=uuid.uuid4(), email="foreign-member@example.com", password_hash="unused")
    foreign_member = OrganizationMember(
        organization=organization_b,
        user=foreign_user,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add(foreign_member)
    database.commit()
    client = TestClient(app)
    owner_a_headers = headers(owner_a, organization_a)

    for method, path, payload in (
        ("patch", f"/team/members/{foreign_member.id}", {"role": "member"}),
        ("delete", f"/team/members/{foreign_member.id}", None),
        ("post", f"/team/members/{foreign_member.id}/password-reset", None),
    ):
        response = client.request(method.upper(), path, headers=owner_a_headers, json=payload)
        assert response.status_code == 403

    foreign_invitation = client.post(
        "/team/invitations",
        headers=headers(owner_b, organization_b),
        json={"email": "foreign-invite@example.com", "role": "member"},
    ).json()
    for action in ("resend", "revoke"):
        response = client.post(
            f"/team/invitations/{foreign_invitation['id']}/{action}",
            headers=owner_a_headers,
        )
        assert response.status_code in {400, 404}


def test_inactive_membership_cannot_be_password_reset_target(database: Session) -> None:
    owner, organization = seed(database)
    inactive_user = User(id=uuid.uuid4(), email="inactive@example.com", password_hash="unused")
    inactive_member = OrganizationMember(
        organization=organization,
        user=inactive_user,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.INACTIVE,
    )
    database.add(inactive_member)
    database.commit()

    response = TestClient(app).post(
        f"/team/members/{inactive_member.id}/password-reset",
        headers=headers(owner, organization),
    )
    assert response.status_code == 403
    assert inactive_user.force_password_change is False


@pytest.mark.parametrize("role", [OrganizationMemberRole.REVIEWER, OrganizationMemberRole.VIEWER])
def test_legacy_roles_can_read_team_members_and_pending_invitations(
    database: Session, role: OrganizationMemberRole
) -> None:
    owner, organization = seed(database, email=f"owner-{role.value}@example.com")
    legacy_user = User(id=uuid.uuid4(), email=f"{role.value}@example.com", password_hash="unused")
    legacy_member = OrganizationMember(
        organization=organization,
        user=legacy_user,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add(legacy_member)
    database.commit()
    client = TestClient(app)
    invitation = client.post(
        "/team/invitations",
        headers=headers(owner, organization),
        json={"email": f"pending-{role.value}@example.com", "role": "member"},
    )
    assert invitation.status_code == 201

    legacy_headers = headers(legacy_user, organization)
    members = client.get("/team/members", headers=legacy_headers)
    pending = client.get("/team/invitations", headers=legacy_headers)
    assert members.status_code == 200
    assert pending.status_code == 200
    assert any(item["user_id"] == str(legacy_user.id) for item in members.json())
    assert pending.json()[0]["invited_email"] == f"pending-{role.value}@example.com"
