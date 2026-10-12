"""Tenant-scoped company profile settings."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import Organization, OrganizationMemberRole, User
from apps.api.app.security_audit import add_security_event
from apps.api.app.tenant import TenantContext, TenantPermission, require_tenant_permission

router = APIRouter(prefix="/organizations", tags=["organization-settings"])


class OrganizationProfileResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str


class OrganizationProfileUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=255)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def _organization(tenant: TenantContext) -> Organization:
    if tenant.organization is None or tenant.membership is None:
        raise HTTPException(status_code=400, detail="Select a workspace first.")
    return tenant.organization


@router.get("/current", response_model=OrganizationProfileResponse)
def get_current_organization(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
) -> OrganizationProfileResponse:
    organization = _organization(tenant)
    return OrganizationProfileResponse(
        id=organization.id, name=organization.name, slug=organization.slug
    )


@router.patch("/current", response_model=OrganizationProfileResponse)
def update_current_organization(
    payload: OrganizationProfileUpdate,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> OrganizationProfileResponse:
    organization = _organization(tenant)
    if tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(
            status_code=403,
            detail="Only workspace owners and admins can edit this profile.",
        )

    if organization.name != payload.name:
        organization.name = payload.name
        add_security_event(
            database,
            organization_id=organization.id,
            actor_user_id=current_user.id,
            action="organization_profile_updated",
            resource_type="organization",
            resource_id=organization.id,
        )
        database.commit()
        database.refresh(organization)

    return OrganizationProfileResponse(
        id=organization.id, name=organization.name, slug=organization.slug
    )
