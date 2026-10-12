"""Enterprise workspace security controls and session lifecycle endpoints."""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationSamlConfiguration,
    OrganizationSecurityAuditEvent,
    OrganizationStatus,
    User,
    UserSession,
)
from apps.api.app.plans import CAPABILITY_SAML_SSO, resolve_plan_entitlements
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.security_audit import add_security_event
from apps.api.app.tenant import (
    TenantContext,
    TenantPermission,
    require_tenant_permission,
)

router = APIRouter(tags=["enterprise security"])


class SsoPolicyUpdate(BaseModel):
    enabled: bool
    rotate_recovery_code: bool = False


class SsoPolicyResponse(BaseModel):
    enabled: bool
    recovery_code: str | None = None


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    sso_authenticated: bool
    current: bool


class AuditEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    subject_user_id: uuid.UUID | None
    action: str
    outcome: str
    resource_type: str | None
    resource_id: str | None
    context: dict
    created_at: datetime


class RecoveryRequest(BaseModel):
    code: str = Field(min_length=32, max_length=128)


def _sensitive_limit(
    request: Request,
    current_user: User = Depends(get_current_user),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> None:
    limiter.check(
        "enterprise-security",
        f"{client_identifier(request)}:{current_user.id}",
        10,
        300,
        fail_closed=True,
    )


def _owner(tenant: TenantContext) -> Organization:
    organization = tenant.organization
    if organization is None or tenant.membership is None:
        raise HTTPException(403, "An active workspace is required.")
    if tenant.membership.role is not OrganizationMemberRole.OWNER:
        raise HTTPException(403, "Only the workspace owner can manage this security policy.")
    return organization


def _hash_recovery_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


@router.put(
    "/organizations/security/sso",
    response_model=SsoPolicyResponse,
    dependencies=[Depends(_sensitive_limit)],
)
def update_sso_policy(
    payload: SsoPolicyUpdate,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> SsoPolicyResponse:
    organization = _owner(tenant)
    organization = database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    if organization is None:
        raise HTTPException(404, "Workspace not found.")
    recovery_code = None
    if payload.enabled:
        try:
            entitled = resolve_plan_entitlements(organization.plan_code).includes(
                CAPABILITY_SAML_SSO
            )
        except ValueError:
            entitled = False
        configuration = database.scalar(
            select(OrganizationSamlConfiguration).where(
                OrganizationSamlConfiguration.organization_id == organization.id
            )
        )
        if not entitled or configuration is None or not configuration.enabled:
            raise HTTPException(409, "Enable a valid SAML configuration before requiring SSO.")
        try:
            from apps.api.app.saml_sso import _sp_config

            _sp_config(organization, configuration)
        except (HTTPException, ValueError):
            raise HTTPException(
                409, "The enabled SAML configuration is not currently usable."
            ) from None
        if not organization.sso_enforced or payload.rotate_recovery_code:
            recovery_code = secrets.token_urlsafe(32)
            organization.sso_recovery_code_hash = _hash_recovery_code(recovery_code)
        organization.sso_enforced = True
    else:
        organization.sso_enforced = False
        organization.sso_recovery_code_hash = None
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="sso_recovery_code_rotated"
        if payload.rotate_recovery_code and payload.enabled
        else "sso_policy_changed",
        resource_type="organization",
        resource_id=organization.id,
        context={"enabled": payload.enabled},
    )
    database.commit()
    return SsoPolicyResponse(enabled=organization.sso_enforced, recovery_code=recovery_code)


@router.get("/organizations/security/sso", response_model=SsoPolicyResponse)
def get_sso_policy(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
) -> SsoPolicyResponse:
    return SsoPolicyResponse(enabled=_owner(tenant).sso_enforced)


@router.post("/organizations/security/sso/recover", dependencies=[Depends(_sensitive_limit)])
def recover_sso_policy(
    payload: RecoveryRequest,
    organization_id: uuid.UUID = Header(alias="X-Organization-ID"),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> SsoPolicyResponse:
    """One-use owner recovery disables enforcement; it never grants workspace access."""
    organization = database.scalar(
        select(Organization)
        .where(
            Organization.id == organization_id,
            Organization.status == OrganizationStatus.ACTIVE,
        )
        .with_for_update()
    )
    membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == current_user.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    )
    if organization is None:
        raise HTTPException(403, "Owner SSO recovery was not accepted.")
    owner_match = (
        membership is not None
        and membership.role is OrganizationMemberRole.OWNER
        and not current_user.force_password_change
    )
    valid_code = bool(
        owner_match
        and organization.sso_enforced
        and organization.sso_recovery_code_hash
        and hmac.compare_digest(
            organization.sso_recovery_code_hash, _hash_recovery_code(payload.code)
        )
    )
    if not valid_code:
        if owner_match:
            add_security_event(
                database,
                organization_id=organization.id,
                actor_user_id=current_user.id,
                action="sso_recovery_rejected",
                outcome="denied",
                resource_type="organization",
                resource_id=organization.id,
                context={"reason": "invalid_or_unavailable_recovery"},
            )
            database.commit()
        raise HTTPException(403, "Owner SSO recovery was not accepted.")
    organization.sso_enforced = False
    organization.sso_recovery_code_hash = None
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="sso_policy_recovered",
        resource_type="organization",
        resource_id=organization.id,
        context={"reason": "one_time_owner_recovery"},
    )
    database.commit()
    return SsoPolicyResponse(enabled=False)


@router.get("/auth/sessions", response_model=list[SessionResponse])
def list_sessions(
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[SessionResponse]:
    now = datetime.now(UTC)
    current_id = getattr(current_user, "_current_session_id", None)
    rows = database.scalars(
        select(UserSession)
        .where(
            UserSession.user_id == current_user.id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .order_by(UserSession.created_at.desc())
    ).all()
    return [
        SessionResponse(
            id=row.id,
            created_at=row.created_at,
            last_seen_at=row.last_seen_at,
            expires_at=row.expires_at,
            sso_authenticated=row.sso_organization_id is not None,
            current=row.id == current_id,
        )
        for row in rows
    ]


def _audit_session_revocations(
    database: Session, actor: User, subject_id: uuid.UUID, session_id: uuid.UUID | None = None
) -> None:
    organization_ids = database.scalars(
        select(OrganizationMember.organization_id).where(
            OrganizationMember.user_id == subject_id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    ).all()
    for org_id in organization_ids:
        add_security_event(
            database,
            organization_id=org_id,
            actor_user_id=actor.id,
            subject_user_id=subject_id,
            action="session_revoked",
            resource_type="session" if session_id else "user_sessions",
            resource_id=session_id,
            context={"session_id": str(session_id) if session_id else None},
        )


@router.post("/auth/logout", status_code=204, dependencies=[Depends(_sensitive_limit)])
def logout(
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> Response:
    session_id = getattr(current_user, "_current_session_id", None)
    if session_id is not None:
        database.execute(
            update(UserSession)
            .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
            .values(revoked_at=datetime.now(UTC))
        )
        _audit_session_revocations(database, current_user, current_user.id, session_id)
        database.commit()
    return Response(status_code=204)


@router.delete(
    "/auth/sessions/{session_id}", status_code=204, dependencies=[Depends(_sensitive_limit)]
)
def revoke_own_session(
    session_id: uuid.UUID,
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> Response:
    session = database.scalar(
        select(UserSession).where(
            UserSession.id == session_id, UserSession.user_id == current_user.id
        )
    )
    if session is None:
        raise HTTPException(404, "Session not found.")
    if session.revoked_at is None:
        session.revoked_at = datetime.now(UTC)
        _audit_session_revocations(database, current_user, current_user.id, session.id)
        database.commit()
        from apps.api.app.security_notifications import notify

        notify(database, current_user, "sessions_revoked", event_id=session.id)
        database.commit()
    return Response(status_code=204)


@router.post(
    "/auth/sessions/revoke-others", status_code=204, dependencies=[Depends(_sensitive_limit)]
)
def revoke_other_sessions(
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> Response:
    current_id = getattr(current_user, "_current_session_id", None)
    now = datetime.now(UTC)
    revoked = database.scalars(
        update(UserSession)
        .where(
            UserSession.user_id == current_user.id,
            UserSession.id != current_id,
            UserSession.revoked_at.is_(None),
            UserSession.expires_at > now,
        )
        .values(revoked_at=now)
        .returning(UserSession.id)
    ).all()
    _audit_session_revocations(database, current_user, current_user.id)
    database.commit()
    if revoked:
        from apps.api.app.security_notifications import notify

        notify(database, current_user, "sessions_revoked")
        database.commit()
    return Response(status_code=204)


@router.delete(
    "/organizations/security/sessions/{session_id}",
    status_code=204,
    dependencies=[Depends(_sensitive_limit)],
)
def admin_revoke_session(
    session_id: uuid.UUID,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> Response:
    if (
        tenant.organization is None
        or tenant.membership is None
        or tenant.membership.role
        not in {OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN}
    ):
        raise HTTPException(403, "Only workspace administrators can revoke member sessions.")
    session = database.scalar(
        select(UserSession).where(UserSession.id == session_id).with_for_update()
    )
    target = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == tenant.organization.id,
            OrganizationMember.user_id == (session.user_id if session else uuid.UUID(int=0)),
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    )
    if session is None or target is None or target.role is OrganizationMemberRole.OWNER:
        raise HTTPException(404, "Session not found.")
    if tenant.membership.role is OrganizationMemberRole.ADMIN:
        if target.role is not OrganizationMemberRole.MEMBER:
            raise HTTPException(404, "Session not found.")
    already_revoked = session.revoked_at is not None
    session.revoked_at = session.revoked_at or datetime.now(UTC)
    add_security_event(
        database,
        organization_id=tenant.organization.id,
        actor_user_id=current_user.id,
        subject_user_id=session.user_id,
        action="session_revoked",
        resource_type="session",
        resource_id=session.id,
        context={"session_id": str(session.id)},
    )
    database.commit()
    if not already_revoked:
        from apps.api.app.security_notifications import notify

        subject = database.get(User, session.user_id)
        if subject is not None:
            notify(database, subject, "sessions_revoked", event_id=session.id)
            database.commit()
    return Response(status_code=204)


@router.get("/organizations/security/audit-events", response_model=list[AuditEventResponse])
def list_security_audit_events(
    limit: int = 100,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[AuditEventResponse]:
    if (
        tenant.organization is None
        or tenant.membership is None
        or tenant.membership.role
        not in {OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN}
    ):
        raise HTTPException(403, "Only workspace administrators can review security events.")
    limit = min(max(limit, 1), 200)
    rows = database.scalars(
        select(OrganizationSecurityAuditEvent)
        .where(OrganizationSecurityAuditEvent.organization_id == tenant.organization.id)
        .order_by(OrganizationSecurityAuditEvent.created_at.desc())
        .limit(limit)
    ).all()
    return [AuditEventResponse.model_validate(row) for row in rows]
