from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.app.models import Document, Organization
from apps.api.app.plans import resolve_organization_entitlements

STORAGE_LIMIT_REACHED_MESSAGE = "This workspace has reached its source storage limit."


def lock_organization_for_storage(database: Session, organization: Organization) -> Organization:
    locked = database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="Organization not found.")
    return locked


def require_storage_capacity(
    database: Session, organization: Organization, incoming_file_size: int
) -> Organization:
    locked = lock_organization_for_storage(database, organization)
    try:
        storage_limit = resolve_organization_entitlements(locked, database).storage_bytes_limit
    except ValueError:
        raise HTTPException(
            status_code=500, detail="Organization plan configuration is invalid."
        ) from None

    current_usage = (
        database.scalar(
            select(func.coalesce(func.sum(Document.file_size), 0)).where(
                Document.organization_id == locked.id
            )
        )
        or 0
    )
    if storage_limit is None:
        raise HTTPException(status_code=500, detail="Organization plan requires configured quotas.")
    if current_usage + incoming_file_size > storage_limit:
        raise HTTPException(status_code=409, detail=STORAGE_LIMIT_REACHED_MESSAGE)
    return locked
