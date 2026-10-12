"""Authorized delivery metadata only: no recipient, payload, token or ciphertext."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.auth import get_current_user
from apps.api.app.database import get_db
from apps.api.app.models import OrganizationMemberRole, TransactionalEmail, User
from apps.api.app.tenant import TenantContext, get_current_tenant

router = APIRouter(tags=["email delivery"])


def metadata(records):
    return [
        {
            "id": str(row.id),
            "kind": row.kind,
            "status": row.status,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "created_at": row.created_at,
            "completed_at": row.completed_at,
        }
        for row in records
    ]


@router.get("/auth/email-deliveries")
def my_deliveries(
    db: Annotated[Session, Depends(get_db)], user: Annotated[User, Depends(get_current_user)]
):
    return metadata(
        db.scalars(
            select(TransactionalEmail)
            .where(TransactionalEmail.user_id == user.id)
            .order_by(TransactionalEmail.created_at.desc())
            .limit(100)
        )
    )


@router.get("/organization/email-deliveries")
def organization_deliveries(
    db: Annotated[Session, Depends(get_db)],
    tenant: Annotated[TenantContext, Depends(get_current_tenant)],
):
    if (
        tenant.organization is None
        or tenant.membership is None
        or tenant.membership.role
        not in {
            OrganizationMemberRole.OWNER,
            OrganizationMemberRole.ADMIN,
        }
    ):
        raise HTTPException(403, "Organization administration is required.")
    return metadata(
        db.scalars(
            select(TransactionalEmail)
            .where(TransactionalEmail.organization_id == tenant.organization.id)
            .order_by(TransactionalEmail.created_at.desc())
            .limit(100)
        )
    )
