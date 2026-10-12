import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationStatus,
    User,
)
from apps.api.app.plans import DEFAULT_PLAN_CODE


def normalize_organization_name(name: str) -> str:
    normalized = " ".join(name.strip().split())
    if not normalized:
        raise ValueError("Company name must not be blank.")
    return normalized[:255]


def organization_slug_base(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name.casefold()).strip("-")
    return (slug or "workspace")[:80]


def allocate_organization_slug(database: Session, name: str) -> str:
    base = organization_slug_base(name)
    candidate = base
    suffix = 2
    while (
        database.scalar(select(Organization.id).where(Organization.slug == candidate)) is not None
    ):
        candidate = f"{base[: max(1, 100 - len(str(suffix)) - 1)]}-{suffix}"
        suffix += 1
    return candidate


def provision_company(database: Session, *, user: User, company_name: str) -> Organization:
    """Stage a new user's organization and owner membership in the caller's transaction."""
    normalized_name = normalize_organization_name(company_name)
    organization = Organization(
        name=normalized_name,
        slug=allocate_organization_slug(database, normalized_name),
        plan_code=DEFAULT_PLAN_CODE,
        status=OrganizationStatus.ACTIVE,
    )
    database.add(organization)
    database.flush()
    database.add(
        OrganizationMember(
            organization_id=organization.id,
            user_id=user.id,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
    )
    database.flush()
    return organization
