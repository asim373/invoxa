from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.models import Organization, OrganizationMember, OrganizationMemberStatus
from apps.api.app.plans import resolve_organization_entitlements

SEAT_LIMIT_REACHED_MESSAGE = "This workspace has reached its active member limit."
INVITATION_SEAT_LIMIT_REACHED_MESSAGE = (
    "Your workspace has reached its active seat limit. Remove an active member or "
    "upgrade your plan to invite another member."
)


def _require_seat_capacity(database: Session, organization: Organization, message: str) -> None:
    """Lock the organization and require capacity for one active membership."""
    locked_organization = database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    if locked_organization is None:
        raise HTTPException(status_code=404, detail="Organization not found.")

    try:
        active_seat_limit = resolve_organization_entitlements(
            locked_organization, database
        ).active_seat_limit
    except ValueError:
        raise HTTPException(
            status_code=500, detail="Organization plan configuration is invalid."
        ) from None

    active_member_count = (
        database.scalar(
            select(func.count(OrganizationMember.id)).where(
                OrganizationMember.organization_id == locked_organization.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            )
        )
        or 0
    )
    if active_seat_limit is None:
        raise HTTPException(status_code=500, detail="Organization plan requires configured quotas.")
    if active_member_count >= active_seat_limit:
        raise HTTPException(status_code=409, detail=message)


def require_active_seat(database: Session, organization: Organization) -> None:
    _require_seat_capacity(database, organization, SEAT_LIMIT_REACHED_MESSAGE)


def require_invitation_seat_capacity(database: Session, organization: Organization) -> None:
    """Reject new invitations when no active seat is available."""
    _require_seat_capacity(database, organization, INVITATION_SEAT_LIMIT_REACHED_MESSAGE)
