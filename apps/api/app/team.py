# ruff: noqa: E501

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.auth import (
    get_current_user,
    hash_password,
    issue_access_token,
    normalize_email,
)
from apps.api.app.database import get_db
from apps.api.app.email import (
    InvitationEmailSender,
    PasswordResetEmailSender,
    get_invitation_email_sender,
    get_password_reset_email_sender,
)
from apps.api.app.models import (
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    PasswordResetToken,
    User,
    UserSession,
)
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.seat_quota import require_active_seat, require_invitation_seat_capacity
from apps.api.app.security_audit import add_security_event
from apps.api.app.settings import settings
from apps.api.app.tenant import TenantContext, TenantPermission, require_tenant_permission

router = APIRouter(tags=["team"])
INVITATION_TTL = timedelta(days=7)


class InviteRequest(BaseModel):
    email: EmailStr
    role: OrganizationMemberRole


class InviteResponse(BaseModel):
    id: uuid.UUID
    invited_email: EmailStr
    role: OrganizationMemberRole
    status: OrganizationInvitationStatus
    expires_at: datetime
    delivery_status: str
    invitation_token: str | None = None


class MemberResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    email: EmailStr
    role: OrganizationMemberRole
    status: OrganizationMemberStatus
    joined_at: datetime | None


class AcceptExistingRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)


class AcceptNewRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    email: EmailStr
    password: str = Field(min_length=12, max_length=128)


class InvitationPreview(BaseModel):
    organization_name: str
    role: OrganizationMemberRole
    invited_email: EmailStr
    expires_at: datetime


class RoleRequest(BaseModel):
    role: OrganizationMemberRole


class DeliveryResponse(BaseModel):
    detail: str


class AcceptNewResponse(DeliveryResponse):
    access_token: str
    organization_id: uuid.UUID


class PasswordResetResponse(BaseModel):
    detail: str
    reset_token: str | None = None
    temporary_password: str | None = None


def _require_organization(tenant: TenantContext) -> Organization:
    if tenant.organization is None:
        raise HTTPException(
            status_code=400,
            detail="Select an active organization before using team management.",
        )
    return tenant.organization


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _role_allowed(actor: OrganizationMemberRole, target: OrganizationMemberRole) -> bool:
    if target is OrganizationMemberRole.OWNER:
        return False
    target = _commercial_role(target)
    if actor is OrganizationMemberRole.OWNER:
        return target in {
            OrganizationMemberRole.ADMIN,
            OrganizationMemberRole.MEMBER,
        }
    if actor is OrganizationMemberRole.ADMIN:
        return target is OrganizationMemberRole.MEMBER
    return False


def _commercial_role(role: OrganizationMemberRole) -> OrganizationMemberRole:
    """Map historical role names to the non-escalating commercial role."""
    if role in {OrganizationMemberRole.REVIEWER, OrganizationMemberRole.VIEWER}:
        return OrganizationMemberRole.MEMBER
    return role


def _password_reset_allowed(actor: OrganizationMemberRole, target: OrganizationMemberRole) -> bool:
    target = _commercial_role(target)
    if actor is OrganizationMemberRole.OWNER:
        return target in {OrganizationMemberRole.ADMIN, OrganizationMemberRole.MEMBER}
    return actor is OrganizationMemberRole.ADMIN and target is OrganizationMemberRole.MEMBER


def _invitation_or_400(database: Session, token: str) -> OrganizationInvitation:
    invitation = database.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.token_hash == _hash_token(token)
        )
    )
    if invitation is None:
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired.")
    if invitation.status is not OrganizationInvitationStatus.PENDING:
        raise HTTPException(status_code=400, detail="Invitation is no longer available.")
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        invitation.status = OrganizationInvitationStatus.EXPIRED
        database.commit()
        raise HTTPException(status_code=400, detail="Invitation is invalid or expired.")
    return invitation


def _team_security_limit(request: Request, user: User, limiter: RateLimiter) -> None:
    limiter.check(
        "team-security",
        f"{client_identifier(request)}:{user.id}",
        10,
        300,
        fail_closed=True,
    )


def _team_peer_limit(request: Request, limiter: RateLimiter) -> None:
    limiter.check("team-security-peer", client_identifier(request), 30, 300, fail_closed=True)


@router.get("/team/invitations/preview", response_model=InvitationPreview)
def preview_invitation(
    request: Request,
    token: str = Query(min_length=32, max_length=256),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> InvitationPreview:
    _team_peer_limit(request, limiter)
    invitation = _invitation_or_400(database, token)
    organization = database.get(Organization, invitation.organization_id)
    if organization is None or organization.status.value != "active":
        raise HTTPException(status_code=400, detail="This invitation is no longer valid.")
    return InvitationPreview(
        organization_name=organization.name,
        role=invitation.role,
        invited_email=invitation.invited_email,
        expires_at=invitation.expires_at,
    )


def _member_response(member: OrganizationMember) -> MemberResponse:
    return MemberResponse(
        id=member.id,
        user_id=member.user_id,
        email=member.user.email,
        role=member.role,
        status=member.status,
        joined_at=member.joined_at,
    )


@router.post("/team/invitations", response_model=InviteResponse, status_code=201)
def create_invitation(
    payload: InviteRequest,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    sender: InvitationEmailSender = Depends(get_invitation_email_sender),  # noqa: B008
) -> InviteResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None or not _role_allowed(tenant.membership.role, payload.role):
        raise HTTPException(
            status_code=403, detail="You do not have permission to invite this role."
        )
    require_invitation_seat_capacity(database, organization)
    email = normalize_email(str(payload.email))
    existing_user = database.scalar(select(User).where(User.email == email))
    active_member = None
    if existing_user is not None:
        active_member = database.scalar(
            select(OrganizationMember).where(
                OrganizationMember.organization_id == organization.id,
                OrganizationMember.user_id == existing_user.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            )
        )
        if active_member is not None:
            raise HTTPException(status_code=409, detail="This person is already a member.")
    pending = database.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.organization_id == organization.id,
            OrganizationInvitation.invited_email == email,
            OrganizationInvitation.status == OrganizationInvitationStatus.PENDING,
        )
    )
    if pending is not None:
        raise HTTPException(status_code=409, detail="A pending invitation already exists.")
    raw_token = secrets.token_urlsafe(48)
    invitation = OrganizationInvitation(
        organization_id=organization.id,
        invited_email=email,
        role=_commercial_role(payload.role),
        token_hash=_hash_token(raw_token),
        invited_by_user_id=current_user.id,
        expires_at=datetime.now(UTC) + INVITATION_TTL,
    )
    database.add(invitation)
    database.flush()
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="invitation_created",
        resource_type="invitation",
        resource_id=invitation.id,
        context={"role": invitation.role.value},
    )
    try:
        database.commit()
        database.refresh(invitation)
    except IntegrityError:
        database.rollback()
        raise HTTPException(
            status_code=409, detail="A pending invitation already exists."
        ) from None
    return_secret = settings.app_environment.casefold() in {"development", "test"}
    if not return_secret or getattr(sender, "database", None) is not None:
        invitation_url = f"{settings.frontend_base_url.rstrip('/')}/invite/accept?{urlencode({'token': raw_token})}"
        try:
            sender.send_invitation(invitation.invited_email, invitation_url, organization.name)
            database.commit()
        except Exception:
            invitation.status = OrganizationInvitationStatus.REVOKED
            invitation.revoked_at = datetime.now(UTC)
            add_security_event(
                database,
                organization_id=organization.id,
                actor_user_id=current_user.id,
                action="invitation_delivery_failed",
                outcome="failure",
                resource_type="invitation",
                resource_id=invitation.id,
            )
            database.commit()
            raise HTTPException(503, "Invitation delivery is temporarily unavailable.") from None
    return InviteResponse(
        id=invitation.id,
        invited_email=invitation.invited_email,
        role=invitation.role,
        status=invitation.status,
        expires_at=invitation.expires_at,
        delivery_status="development_token_returned" if return_secret else (
            "queued" if getattr(sender, "database", None) is not None else "sent"
        ),
        invitation_token=raw_token if return_secret else None,
    )


@router.get("/team/members", response_model=list[MemberResponse])
def list_members(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[MemberResponse]:
    organization = _require_organization(tenant)
    members = database.scalars(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    ).all()
    return [_member_response(member) for member in members]


@router.get(
    "/team/invitations", response_model=list[InviteResponse], response_model_exclude_none=True
)
def list_invitations(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[InviteResponse]:
    organization = _require_organization(tenant)
    invitations = database.scalars(
        select(OrganizationInvitation).where(
            OrganizationInvitation.organization_id == organization.id,
            OrganizationInvitation.status == OrganizationInvitationStatus.PENDING,
        )
    ).all()
    return [
        InviteResponse(
            id=item.id,
            invited_email=item.invited_email,
            role=item.role,
            status=item.status,
            expires_at=item.expires_at,
            delivery_status="not_sent",
        )
        for item in invitations
    ]


@router.post("/team/invitations/accept", response_model=MemberResponse)
def accept_existing_invitation(
    payload: AcceptExistingRequest,
    request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> MemberResponse:
    _team_security_limit(request, current_user, limiter)
    invitation = _invitation_or_400(database, payload.token)
    organization = database.get(Organization, invitation.organization_id)
    if organization is None or organization.status.value != "active":
        raise HTTPException(status_code=400, detail="This invitation is no longer valid.")
    if normalize_email(current_user.email) != invitation.invited_email:
        raise HTTPException(
            status_code=403, detail="Invitation email does not match the authenticated user."
        )
    existing = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == invitation.organization_id,
            OrganizationMember.user_id == current_user.id,
        )
    )
    try:
        if existing is None:
            require_active_seat(database, organization)
            existing = OrganizationMember(
                organization_id=invitation.organization_id,
                user_id=current_user.id,
                role=invitation.role,
                status=OrganizationMemberStatus.ACTIVE,
                joined_at=datetime.now(UTC),
            )
            database.add(existing)
        elif existing.status is not OrganizationMemberStatus.ACTIVE:
            require_active_seat(database, organization)
            existing.role = invitation.role
            existing.status = OrganizationMemberStatus.ACTIVE
            existing.joined_at = datetime.now(UTC)
        else:
            raise HTTPException(
                status_code=409, detail="This invitation has already been accepted."
            )
        invitation.status = OrganizationInvitationStatus.ACCEPTED
        invitation.accepted_at = datetime.now(UTC)
        add_security_event(
            database,
            organization_id=organization.id,
            actor_user_id=current_user.id,
            subject_user_id=current_user.id,
            action="invitation_accepted",
            resource_type="invitation",
            resource_id=invitation.id,
        )
        database.commit()
        database.refresh(existing)
    except IntegrityError:
        database.rollback()
        raise HTTPException(status_code=409, detail="Invitation could not be accepted.") from None
    return _member_response(existing)


@router.post("/team/invitations/accept-new", status_code=201)
def accept_new_invitation(
    payload: AcceptNewRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> AcceptNewResponse:
    _team_peer_limit(request, limiter)
    invitation = _invitation_or_400(database, payload.token)
    organization = database.get(Organization, invitation.organization_id)
    if organization is None or organization.status.value != "active":
        raise HTTPException(status_code=400, detail="This invitation is no longer valid.")
    if normalize_email(str(payload.email)) != invitation.invited_email:
        raise HTTPException(
            status_code=403, detail="Invitation email does not match the invitation."
        )
    if database.scalar(select(User).where(User.email == invitation.invited_email)) is not None:
        raise HTTPException(
            status_code=409, detail="An account already exists for this invitation email."
        )
    require_active_seat(database, organization)
    user = User(email=invitation.invited_email, password_hash=hash_password(payload.password))
    member = OrganizationMember(
        organization_id=invitation.organization_id,
        user=user,
        role=invitation.role,
        status=OrganizationMemberStatus.ACTIVE,
        joined_at=datetime.now(UTC),
    )
    database.add_all([user, member])
    invitation.status = OrganizationInvitationStatus.ACCEPTED
    invitation.accepted_at = datetime.now(UTC)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=user.id,
        subject_user_id=user.id,
        action="invitation_accepted",
        resource_type="invitation",
        resource_id=invitation.id,
    )
    try:
        database.flush()
        access_token = issue_access_token(database, user)
        database.commit()
    except IntegrityError:
        database.rollback()
        raise HTTPException(status_code=409, detail="Invitation could not be accepted.") from None
    return AcceptNewResponse(
        detail="Account created and invitation accepted.",
        access_token=access_token,
        organization_id=invitation.organization_id,
    )


@router.patch("/team/members/{member_id}", response_model=MemberResponse)
def change_member_role(
    member_id: uuid.UUID,
    payload: RoleRequest,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> MemberResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None or tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(status_code=403, detail="You do not have permission to manage roles.")
    member = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == organization.id,
        )
    )
    if (
        member is not None
        and tenant.membership.role is OrganizationMemberRole.ADMIN
        and member.role is OrganizationMemberRole.ADMIN
    ):
        add_security_event(
            database,
            organization_id=organization.id,
            actor_user_id=current_user.id,
            subject_user_id=member.user_id,
            action="membership_role_change_denied",
            outcome="denied",
            resource_type="membership",
            resource_id=member.id,
            context={"reason": "peer_admin_protected"},
        )
        database.commit()
        raise HTTPException(status_code=403, detail="This membership cannot be changed.")
    if (
        member is None
        or member.user_id == current_user.id
        or member.role is OrganizationMemberRole.OWNER
        or (
            tenant.membership.role is OrganizationMemberRole.ADMIN
            and member.role is not OrganizationMemberRole.MEMBER
        )
    ):
        raise HTTPException(status_code=403, detail="This membership cannot be changed.")
    if not _role_allowed(tenant.membership.role, payload.role):
        raise HTTPException(status_code=403, detail="You cannot assign this role.")
    previous_role = member.role
    member.role = _commercial_role(payload.role)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        subject_user_id=member.user_id,
        action="membership_role_changed",
        resource_type="membership",
        resource_id=member.id,
        context={"previous_role": previous_role.value, "role": member.role.value},
    )
    database.commit()
    database.refresh(member)
    return _member_response(member)


@router.delete("/team/members/{member_id}", response_model=DeliveryResponse)
def deactivate_member(
    member_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> DeliveryResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None or tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(
            status_code=403, detail="You do not have permission to deactivate members."
        )
    member = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == organization.id,
        )
    )
    if (
        member is None
        or member.user_id == current_user.id
        or not _role_allowed(tenant.membership.role, member.role)
    ):
        raise HTTPException(status_code=403, detail="This membership cannot be deactivated.")
    member.status = OrganizationMemberStatus.INACTIVE
    member.deactivated_at = datetime.now(UTC)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        subject_user_id=member.user_id,
        action="membership_deactivated",
        resource_type="membership",
        resource_id=member.id,
    )
    database.commit()
    return DeliveryResponse(detail="Member deactivated.")


@router.post("/team/members/{member_id}/password-reset", response_model=PasswordResetResponse)
def initiate_member_password_reset(
    member_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    email_sender: PasswordResetEmailSender = Depends(get_password_reset_email_sender),  # noqa: B008
) -> PasswordResetResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None:
        raise HTTPException(
            status_code=403, detail="You do not have permission to reset passwords."
        )
    member = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.id == member_id,
            OrganizationMember.organization_id == organization.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    )
    if (
        member is None
        or member.user_id == current_user.id
        or not _password_reset_allowed(tenant.membership.role, member.role)
    ):
        raise HTTPException(status_code=403, detail="You cannot reset this member's password.")
    now = datetime.now(UTC)
    database.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == member.user_id,
        PasswordResetToken.used_at.is_(None),
    ).update({PasswordResetToken.used_at: now}, synchronize_session=False)
    return_secret = settings.app_environment.casefold() in {"development", "test"}
    temporary_password = None
    reset_token_value = None
    reset_token_record = None
    if not return_secret:
        reset_token_value = secrets.token_urlsafe(48)
        member.user.password_hash = hash_password(secrets.token_urlsafe(48))
        member.user.force_password_change = True
        reset_token_record = PasswordResetToken(
            user_id=member.user_id,
            token_hash=hashlib.sha256(reset_token_value.encode("utf-8")).hexdigest(),
            expires_at=now + timedelta(minutes=settings.password_reset_token_expire_minutes),
        )
        database.add(reset_token_record)
    else:
        temporary_password = secrets.token_urlsafe(24)
        member.user.password_hash = hash_password(temporary_password)
        member.user.force_password_change = True
    member.user.auth_version = (member.user.auth_version or 0) + 1
    database.query(UserSession).filter(
        UserSession.user_id == member.user_id,
        UserSession.revoked_at.is_(None),
    ).update({UserSession.revoked_at: now}, synchronize_session=False)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        subject_user_id=member.user_id,
        action="administrative_password_reset_initiated",
        resource_type="user",
        resource_id=member.user_id,
    )
    database.commit()
    if not return_secret and reset_token_value is not None:
        assert reset_token_record is not None
        reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password?{urlencode({'token': reset_token_value})}"
        try:
            email_sender.send_password_reset(member.user.email, reset_url)
            database.commit()
        except Exception:
            reset_token_record.used_at = datetime.now(UTC)
            add_security_event(
                database,
                organization_id=organization.id,
                actor_user_id=current_user.id,
                subject_user_id=member.user_id,
                action="administrative_password_reset_delivery_failed",
                outcome="failure",
                resource_type="user",
                resource_id=member.user_id,
            )
            database.commit()
            raise HTTPException(
                503, "Password reset delivery is temporarily unavailable."
            ) from None
    return PasswordResetResponse(
        detail="A password reset was issued."
        if not return_secret
        else "A temporary password was generated. Share it securely; it is shown once.",
        temporary_password=temporary_password,
    )


@router.post("/team/invitations/{invitation_id}/revoke", response_model=DeliveryResponse)
def revoke_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> DeliveryResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None or tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(
            status_code=403, detail="You do not have permission to revoke invitations."
        )
    invitation = database.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == organization.id,
        )
    )
    if invitation is None:
        raise HTTPException(status_code=404, detail="Invitation not found.")
    if invitation.status is not OrganizationInvitationStatus.PENDING:
        raise HTTPException(status_code=400, detail="Invitation is no longer available.")
    invitation.status = OrganizationInvitationStatus.REVOKED
    invitation.revoked_at = datetime.now(UTC)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="invitation_revoked",
        resource_type="invitation",
        resource_id=invitation.id,
    )
    database.commit()
    return DeliveryResponse(detail="Invitation revoked.")


@router.post("/team/invitations/{invitation_id}/resend", response_model=InviteResponse)
def resend_invitation(
    invitation_id: uuid.UUID,
    request: Request,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    sender: InvitationEmailSender = Depends(get_invitation_email_sender),  # noqa: B008
) -> InviteResponse:
    _team_security_limit(request, current_user, limiter)
    organization = _require_organization(tenant)
    if tenant.membership is None or tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(
            status_code=403, detail="You do not have permission to resend invitations."
        )
    invitation = database.scalar(
        select(OrganizationInvitation).where(
            OrganizationInvitation.id == invitation_id,
            OrganizationInvitation.organization_id == organization.id,
        )
    )
    if invitation is None or invitation.status is not OrganizationInvitationStatus.PENDING:
        raise HTTPException(status_code=400, detail="Invitation is no longer available.")
    raw_token = secrets.token_urlsafe(48)
    invitation.token_hash = _hash_token(raw_token)
    invitation.expires_at = datetime.now(UTC) + INVITATION_TTL
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="invitation_resent",
        resource_type="invitation",
        resource_id=invitation.id,
    )
    database.commit()
    database.refresh(invitation)
    return_secret = settings.app_environment.casefold() in {"development", "test"}
    if not return_secret or getattr(sender, "database", None) is not None:
        invitation_url = f"{settings.frontend_base_url.rstrip('/')}/invite/accept?{urlencode({'token': raw_token})}"
        try:
            sender.send_invitation(invitation.invited_email, invitation_url, organization.name)
            database.commit()
        except Exception:
            invitation.status = OrganizationInvitationStatus.REVOKED
            invitation.revoked_at = datetime.now(UTC)
            add_security_event(
                database,
                organization_id=organization.id,
                actor_user_id=current_user.id,
                action="invitation_delivery_failed",
                outcome="failure",
                resource_type="invitation",
                resource_id=invitation.id,
            )
            database.commit()
            raise HTTPException(503, "Invitation delivery is temporarily unavailable.") from None
    return InviteResponse(
        id=invitation.id,
        invited_email=invitation.invited_email,
        role=invitation.role,
        status=invitation.status,
        expires_at=invitation.expires_at,
        delivery_status="development_token_returned" if return_secret else (
            "queued" if getattr(sender, "database", None) is not None else "sent"
        ),
        invitation_token=raw_token if return_secret else None,
    )
