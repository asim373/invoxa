"""Tenant-scoped durable security audit helpers."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from apps.api.app.models import OrganizationSecurityAuditEvent

ALLOWED_CONTEXT_KEYS = {
    "role",
    "previous_role",
    "status",
    "enabled",
    "credential_id",
    "invitation_id",
    "session_id",
    "reason",
}


def add_security_event(
    database: Session,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID | None,
    action: str,
    outcome: str = "success",
    subject_user_id: uuid.UUID | None = None,
    resource_type: str | None = None,
    resource_id: str | uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
) -> OrganizationSecurityAuditEvent:
    safe_context = {
        key: value
        for key, value in (context or {}).items()
        if key in ALLOWED_CONTEXT_KEYS and isinstance(value, (str, int, bool, type(None)))
    }
    event = OrganizationSecurityAuditEvent(
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        subject_user_id=subject_user_id,
        action=action[:80],
        outcome=outcome[:24],
        resource_type=resource_type[:80] if resource_type else None,
        resource_id=str(resource_id)[:128] if resource_id is not None else None,
        context=safe_context,
    )
    database.add(event)
    return event
