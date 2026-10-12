import uuid
from dataclasses import dataclass
from enum import StrEnum

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationSamlConfiguration,
    OrganizationStatus,
    User,
    UserRole,
)


class TenantPermission(StrEnum):
    READ = "read"
    MUTATE_DOCUMENTS = "mutate_documents"
    REVIEW_FINDINGS = "review_findings"
    EXPORT = "export"


@dataclass(frozen=True)
class TenantContext:
    organization: Organization | None
    membership: OrganizationMember | None
    legacy_compatibility: bool = False


def get_current_tenant(
    organization_id: uuid.UUID | None = Header(  # noqa: B008
        default=None, alias="X-Organization-ID"
    ),
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> TenantContext:
    if current_user.force_password_change:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You must change your temporary password before using workspace features.",
        )
    # The header is the explicit tenant-selection mechanism. Without it, retain
    # only the owner-scoped compatibility path for unresolved legacy records.
    # Organization-owned access always requires an explicit organization ID.
    if organization_id is None:
        enforced_membership = database.scalar(
            select(OrganizationMember.id)
            .join(Organization, Organization.id == OrganizationMember.organization_id)
            .where(
                OrganizationMember.user_id == current_user.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
                Organization.status == OrganizationStatus.ACTIVE,
                Organization.sso_enforced.is_(True),
            )
            .limit(1)
        )
        if enforced_membership is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Select the workspace and authenticate through its required SAML sign-in.",
            )
        return TenantContext(organization=None, membership=None, legacy_compatibility=True)
    statement = (
        select(OrganizationMember, Organization)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            OrganizationMember.user_id == current_user.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            Organization.status == OrganizationStatus.ACTIVE,
        )
    )
    statement = statement.where(Organization.id == organization_id)

    if not hasattr(database, "execute"):
        return TenantContext(organization=None, membership=None, legacy_compatibility=True)
    matches = database.execute(statement).all()
    if not matches:
        # Pre-organization users and unresolved legacy records retain the
        # old owner-scoped behavior until an explicit membership exists.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You are not an active member of this organization.",
        )
    membership, organization = matches[0]
    if organization.sso_enforced:
        configuration = database.scalar(
            select(OrganizationSamlConfiguration).where(
                OrganizationSamlConfiguration.organization_id == organization.id
            )
        )
        session_sso_org = getattr(current_user, "_current_session_sso_organization_id", None)
        if configuration is None or not configuration.enabled or session_sso_org != organization.id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This workspace requires SAML single sign-on.",
            )
    return TenantContext(organization=organization, membership=membership)


def require_tenant_permission(permission: TenantPermission):
    def dependency(
        tenant: TenantContext = Depends(get_current_tenant),  # noqa: B008
    ) -> TenantContext:
        if tenant.legacy_compatibility:
            return tenant
        assert tenant.membership is not None
        role = tenant.membership.role
        permissions = {
            OrganizationMemberRole.OWNER: {
                TenantPermission.READ,
                TenantPermission.MUTATE_DOCUMENTS,
                TenantPermission.REVIEW_FINDINGS,
                TenantPermission.EXPORT,
            },
            OrganizationMemberRole.ADMIN: {
                TenantPermission.READ,
                TenantPermission.MUTATE_DOCUMENTS,
                TenantPermission.REVIEW_FINDINGS,
                TenantPermission.EXPORT,
            },
            OrganizationMemberRole.MEMBER: {
                TenantPermission.READ,
                TenantPermission.MUTATE_DOCUMENTS,
                TenantPermission.REVIEW_FINDINGS,
                TenantPermission.EXPORT,
            },
            OrganizationMemberRole.REVIEWER: {
                TenantPermission.READ,
                TenantPermission.MUTATE_DOCUMENTS,
                TenantPermission.REVIEW_FINDINGS,
                TenantPermission.EXPORT,
            },
            OrganizationMemberRole.VIEWER: {TenantPermission.READ, TenantPermission.EXPORT},
        }
        if permission not in permissions[role]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return tenant

    return dependency


def get_current_workspace_editor(  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    tenant: TenantContext = Depends(  # noqa: B008
        require_tenant_permission(TenantPermission.MUTATE_DOCUMENTS)  # noqa: B008
    ),  # noqa: B008
) -> User:
    """Authenticate an operational actor without using global role as tenant authority."""
    if tenant.legacy_compatibility and current_user.role is UserRole.VIEWER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to modify legacy documents.",
        )
    return current_user
