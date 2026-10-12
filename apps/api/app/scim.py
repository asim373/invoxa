"""Enterprise-only SCIM credential and configuration foundation."""
# ruff: noqa: E501, B008

import hashlib
import json
import re
import secrets
import uuid
from datetime import UTC, datetime

from email_validator import EmailNotValidError, validate_email
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.app.auth import hash_password, normalize_email
from apps.api.app.database import get_db
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    ScimConfiguration,
    ScimCredential,
    ScimProvisioningEvent,
    ScimUserMapping,
    User,
)
from apps.api.app.plans import CAPABILITY_SCIM, resolve_plan_entitlements
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.seat_quota import require_active_seat
from apps.api.app.security_audit import add_security_event
from apps.api.app.settings import settings
from apps.api.app.tenant import TenantContext, get_current_tenant

FEATURE_BLOCKED = (
    "This feature is not included in your current plan. Upgrade your plan to access it."
)
TOKEN_PREFIX = "invx_scim_"


def _management_limit(request: Request, limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    limiter.check("scim-management", client_identifier(request), 30, 300, fail_closed=True)


management_router = APIRouter(
    prefix="/organizations/scim",
    tags=["SCIM configuration"],
    dependencies=[Depends(_management_limit)],
)


def _scim_peer_limit(request: Request, limiter: RateLimiter = Depends(get_rate_limiter)) -> None:
    limiter.check("scim-peer", client_identifier(request), 300, 60, fail_closed=True)


scim_router = APIRouter(prefix="/scim/v2", tags=["SCIM"], dependencies=[Depends(_scim_peer_limit)])


class ScimConfigurationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    enabled: bool
    created_at: datetime
    updated_at: datetime


class ScimConfigurationUpdate(BaseModel):
    enabled: bool = True


class ScimCredentialCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)


class ScimCredentialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ScimCredentialCreatedResponse(ScimCredentialResponse):
    secret: str


def _owner(tenant: TenantContext) -> Organization:
    organization = tenant.organization
    if organization is None or tenant.membership is None:
        raise HTTPException(403, "An active organization is required.")
    try:
        allowed = resolve_plan_entitlements(organization.plan_code).includes(CAPABILITY_SCIM)
    except ValueError:
        allowed = False
    if not allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, FEATURE_BLOCKED)
    if tenant.membership.role is not OrganizationMemberRole.OWNER:
        raise HTTPException(403, "Only the workspace owner can manage SCIM.")
    return organization


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


@management_router.get("", response_model=ScimConfigurationResponse)
def get_scim_configuration(
    tenant: TenantContext = Depends(get_current_tenant), database: Session = Depends(get_db)
) -> ScimConfigurationResponse:
    organization = _owner(tenant)
    configuration = database.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    if configuration is None:
        raise HTTPException(404, "SCIM is not configured.")
    return ScimConfigurationResponse.model_validate(configuration)


@management_router.put("", response_model=ScimConfigurationResponse)
def update_scim_configuration(
    payload: ScimConfigurationUpdate,
    tenant: TenantContext = Depends(get_current_tenant),
    database: Session = Depends(get_db),
) -> ScimConfigurationResponse:
    organization = _owner(tenant)
    configuration = database.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    if configuration is None:
        configuration = ScimConfiguration(organization_id=organization.id)
        database.add(configuration)
    configuration.enabled = payload.enabled
    database.flush()
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=tenant.membership.user_id if tenant.membership else None,
        action="scim_configuration_changed",
        resource_type="scim_configuration",
        resource_id=configuration.id,
        context={"enabled": payload.enabled},
    )
    database.commit()
    database.refresh(configuration)
    return ScimConfigurationResponse.model_validate(configuration)


@management_router.delete("", status_code=204)
def disable_scim_configuration(
    tenant: TenantContext = Depends(get_current_tenant), database: Session = Depends(get_db)
) -> None:
    organization = _owner(tenant)
    configuration = database.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    if configuration is not None:
        configuration.enabled = False
        add_security_event(
            database,
            organization_id=organization.id,
            actor_user_id=tenant.membership.user_id if tenant.membership else None,
            action="scim_configuration_disabled",
            resource_type="scim_configuration",
            resource_id=configuration.id,
        )
        database.commit()


@management_router.post(
    "/credentials", response_model=ScimCredentialCreatedResponse, status_code=201
)
def create_scim_credential(
    payload: ScimCredentialCreate,
    tenant: TenantContext = Depends(get_current_tenant),
    database: Session = Depends(get_db),
) -> ScimCredentialCreatedResponse:
    organization = _owner(tenant)
    if database.scalar(
        select(ScimCredential).where(
            ScimCredential.organization_id == organization.id, ScimCredential.label == payload.label
        )
    ):
        raise HTTPException(409, "A SCIM credential with this label already exists.")
    token = _new_token()
    credential = ScimCredential(
        organization_id=organization.id, label=payload.label, secret_hash=_hash(token)
    )
    database.add(credential)
    database.flush()
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=tenant.membership.user_id if tenant.membership else None,
        action="scim_credential_created",
        resource_type="scim_credential",
        resource_id=credential.id,
        context={"credential_id": str(credential.id)},
    )
    database.commit()
    database.refresh(credential)
    return ScimCredentialCreatedResponse(
        **ScimCredentialResponse.model_validate(credential).model_dump(), secret=token
    )


@management_router.get("/credentials", response_model=list[ScimCredentialResponse])
def list_scim_credentials(
    tenant: TenantContext = Depends(get_current_tenant), database: Session = Depends(get_db)
) -> list[ScimCredentialResponse]:
    organization = _owner(tenant)
    credentials = database.scalars(
        select(ScimCredential)
        .where(ScimCredential.organization_id == organization.id)
        .order_by(ScimCredential.created_at)
    ).all()
    return [ScimCredentialResponse.model_validate(item) for item in credentials]


@management_router.delete("/credentials/{credential_id}", response_model=ScimCredentialResponse)
def revoke_scim_credential(
    credential_id: uuid.UUID,
    tenant: TenantContext = Depends(get_current_tenant),
    database: Session = Depends(get_db),
) -> ScimCredentialResponse:
    organization = _owner(tenant)
    credential = database.scalar(
        select(ScimCredential).where(
            ScimCredential.id == credential_id, ScimCredential.organization_id == organization.id
        )
    )
    if credential is None:
        raise HTTPException(404, "SCIM credential not found.")
    credential.revoked_at = datetime.now(UTC)
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=tenant.membership.user_id if tenant.membership else None,
        action="scim_credential_revoked",
        resource_type="scim_credential",
        resource_id=credential.id,
        context={"credential_id": str(credential.id)},
    )
    database.commit()
    database.refresh(credential)
    return ScimCredentialResponse.model_validate(credential)


@management_router.post(
    "/credentials/{credential_id}/rotate", response_model=ScimCredentialCreatedResponse
)
def rotate_scim_credential(
    credential_id: uuid.UUID,
    tenant: TenantContext = Depends(get_current_tenant),
    database: Session = Depends(get_db),
) -> ScimCredentialCreatedResponse:
    organization = _owner(tenant)
    credential = database.scalar(
        select(ScimCredential)
        .where(
            ScimCredential.id == credential_id, ScimCredential.organization_id == organization.id
        )
        .with_for_update()
    )
    if credential is None:
        raise HTTPException(404, "SCIM credential not found.")
    token = _new_token()
    credential.secret_hash = _hash(token)
    credential.revoked_at = None
    credential.last_used_at = None
    database.commit()
    database.refresh(credential)
    return ScimCredentialCreatedResponse(
        **ScimCredentialResponse.model_validate(credential).model_dump(), secret=token
    )


def get_scim_principal(
    authorization: str | None = Header(default=None),
    database: Session = Depends(get_db),
    limiter: RateLimiter = Depends(get_rate_limiter),
) -> tuple[ScimCredential, Organization]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Invalid SCIM credential.")
    token = authorization[7:]
    if len(token) > 256 or not token.startswith(TOKEN_PREFIX):
        raise HTTPException(401, "Invalid SCIM credential.")
    credential = database.scalar(
        select(ScimCredential).where(ScimCredential.secret_hash == _hash(token))
    )
    if credential is None or credential.revoked_at is not None:
        raise HTTPException(401, "Invalid SCIM credential.")
    organization = database.get(Organization, credential.organization_id)
    if organization is None or organization.status.value != "active":
        raise HTTPException(403, "SCIM is unavailable for this organization.")
    configuration = database.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    try:
        allowed = resolve_plan_entitlements(organization.plan_code).includes(CAPABILITY_SCIM)
    except ValueError:
        allowed = False
    if configuration is None or not configuration.enabled or not allowed:
        raise HTTPException(403, FEATURE_BLOCKED)
    if isinstance(limiter, RateLimiter):
        limiter.check("scim-credential", str(credential.id), 120, 60, fail_closed=True)
    credential.last_used_at = datetime.now(UTC)
    database.commit()
    return credential, organization


SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
SCIM_LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
SCIM_ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SCIM_SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"
SCIM_RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"


class _PatchError(ValueError):
    def __init__(self, detail: str, scim_type: str):
        super().__init__(detail)
        self.scim_type = scim_type


MAX_PAGE_SIZE = 100
MAX_FILTER_LENGTH = 512
MAX_SCIM_BODY_BYTES = 65_536
FILTER_PATTERN = re.compile(
    r'^(userName|externalId|emails\[type\s+eq\s+"work"\]\.value)\s+eq\s+("(?:[^"\\]|\\.)*")$',
    re.IGNORECASE,
)


def _scim_error(status_code: int, detail: str, scim_type: str | None = None) -> JSONResponse:
    error: dict[str, object] = {
        "schemas": [SCIM_ERROR_SCHEMA],
        "status": str(status_code),
        "detail": detail,
    }
    if scim_type:
        error["scimType"] = scim_type
    return JSONResponse(status_code=status_code, content=error, media_type="application/scim+json")


async def _scim_json_body(request: Request) -> object:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().casefold()
    if content_type not in {"application/scim+json", "application/json"}:
        raise HTTPException(415, "Unsupported SCIM content type.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_SCIM_BODY_BYTES:
            raise HTTPException(413, "SCIM request body is too large.")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "Malformed SCIM JSON payload.") from None


def _parse_filter(value: str | None) -> tuple[str, str] | None:
    if value is None:
        return None
    if len(value) > MAX_FILTER_LENGTH:
        raise ValueError("invalidFilter")
    match = FILTER_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("invalidFilter")
    try:
        parsed = json.loads(match.group(2))
    except json.JSONDecodeError:
        raise ValueError("invalidFilter") from None
    if not isinstance(parsed, str) or not parsed or len(parsed) > 320:
        raise ValueError("invalidFilter")
    attribute = match.group(1).casefold()
    if attribute in {"username", 'emails[type eq "work"].value'}:
        parsed = parsed.strip().casefold()
    return attribute, parsed


def _user_resource(
    request: Request, mapping: ScimUserMapping, user: User, membership: OrganizationMember
) -> dict[str, object]:
    base = _resource_base()
    return {
        "schemas": [SCIM_USER_SCHEMA],
        "id": str(mapping.id),
        "externalId": mapping.external_id,
        "userName": mapping.user_name,
        "active": membership.status is OrganizationMemberStatus.ACTIVE and user.is_active,
        "emails": [{"value": mapping.work_email or user.email, "type": "work", "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": mapping.created_at.isoformat(),
            "lastModified": mapping.updated_at.isoformat(),
            "location": f"{base}/Users/{mapping.id}",
        },
    }


def _resource_base() -> str:
    if not settings.scim_public_base_url:
        raise HTTPException(503, "SCIM public URL configuration is unavailable.")
    return settings.scim_public_base_url.rstrip("/") + "/scim/v2"


@scim_router.get("/ServiceProviderConfig")
def service_provider_config(
    _: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
) -> JSONResponse:
    return JSONResponse(
        content={
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False},
            "filter": {"supported": True, "maxResults": MAX_PAGE_SIZE},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "SCIM Bearer Token",
                    "description": "Organization-scoped SCIM bearer credential",
                    "primary": True,
                }
            ],
        },
        media_type="application/scim+json",
    )


@scim_router.get("/Schemas")
def schemas(_: tuple[ScimCredential, Organization] = Depends(get_scim_principal)) -> JSONResponse:
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [
                {
                    "schemas": [SCIM_SCHEMA_SCHEMA],
                    "id": SCIM_USER_SCHEMA,
                    "name": "User",
                    "description": "SCIM User resource",
                    "attributes": [
                        {
                            "name": "userName",
                            "type": "string",
                            "multiValued": False,
                            "required": True,
                            "caseExact": False,
                            "mutability": "readWrite",
                            "returned": "always",
                            "uniqueness": "server",
                        },
                        {
                            "name": "externalId",
                            "type": "string",
                            "multiValued": False,
                            "required": False,
                            "caseExact": True,
                            "mutability": "readWrite",
                            "returned": "default",
                            "uniqueness": "server",
                        },
                        {
                            "name": "active",
                            "type": "boolean",
                            "multiValued": False,
                            "required": True,
                            "mutability": "readWrite",
                            "returned": "always",
                        },
                        {
                            "name": "emails",
                            "type": "complex",
                            "multiValued": True,
                            "required": False,
                            "subAttributes": [
                                {
                                    "name": "value",
                                    "type": "string",
                                    "multiValued": False,
                                    "required": True,
                                    "caseExact": False,
                                    "mutability": "readWrite",
                                    "returned": "default",
                                },
                                {
                                    "name": "type",
                                    "type": "string",
                                    "multiValued": False,
                                    "required": False,
                                    "caseExact": False,
                                    "canonicalValues": ["work"],
                                    "mutability": "immutable",
                                    "returned": "default",
                                },
                                {
                                    "name": "primary",
                                    "type": "boolean",
                                    "multiValued": False,
                                    "required": False,
                                    "mutability": "immutable",
                                    "returned": "default",
                                },
                            ],
                            "mutability": "readWrite",
                            "returned": "default",
                        },
                    ],
                }
            ],
        },
        media_type="application/scim+json",
    )


@scim_router.get("/ResourceTypes")
def resource_types(
    _: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
) -> JSONResponse:
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": 1,
            "startIndex": 1,
            "itemsPerPage": 1,
            "Resources": [
                {
                    "schemas": [SCIM_RESOURCE_TYPE_SCHEMA],
                    "id": "User",
                    "name": "User",
                    "endpoint": "/Users",
                    "schema": SCIM_USER_SCHEMA,
                }
            ],
        },
        media_type="application/scim+json",
    )


@scim_router.get("/Users")
def list_users(
    request: Request,
    filter: str | None = None,
    start_index: str = Query(default="1", alias="startIndex"),
    count: str = "100",
    principal: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
    database: Session = Depends(get_db),
) -> JSONResponse:
    try:
        start_index_value = int(start_index)
        count_value = int(count)
    except ValueError:
        return _scim_error(400, "Invalid pagination parameters.", "invalidValue")
    if start_index_value < 1 or count_value < 0 or count_value > MAX_PAGE_SIZE:
        return _scim_error(400, "Invalid pagination parameters.", "invalidValue")
    credential, organization = principal
    statement = (
        select(ScimUserMapping, User, OrganizationMember)
        .join(User, User.id == ScimUserMapping.user_id)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == ScimUserMapping.organization_id)
            & (OrganizationMember.user_id == ScimUserMapping.user_id),
        )
        .where(
            ScimUserMapping.organization_id == organization.id,
            ScimUserMapping.deactivated_at.is_(None),
            OrganizationMember.status.in_(
                [OrganizationMemberStatus.ACTIVE, OrganizationMemberStatus.INACTIVE]
            ),
        )
    )
    try:
        parsed_filter = _parse_filter(filter)
    except ValueError:
        return _scim_error(400, "The filter is invalid or unsupported.", "invalidFilter")
    if parsed_filter:
        attribute, value = parsed_filter
        if attribute == "username":
            statement = statement.where(func.lower(ScimUserMapping.user_name) == value)
        elif attribute == "externalid":
            statement = statement.where(ScimUserMapping.external_id == value)
        else:
            statement = statement.where(
                func.lower(func.coalesce(ScimUserMapping.work_email, User.email)) == value
            )
    total = database.scalar(select(func.count()).select_from(statement.subquery())) or 0
    page = database.execute(
        statement.order_by(ScimUserMapping.id).offset(start_index_value - 1).limit(count_value)
    ).all()
    resources = [_user_resource(request, mapping, user, member) for mapping, user, member in page]
    credential.last_used_at = datetime.now(UTC)
    database.commit()
    return JSONResponse(
        content={
            "schemas": [SCIM_LIST_SCHEMA],
            "totalResults": total,
            "startIndex": start_index_value,
            "itemsPerPage": len(resources),
            "Resources": resources,
        },
        media_type="application/scim+json",
    )


@scim_router.get("/Users/{resource_id}")
def get_user(
    resource_id: str,
    request: Request,
    principal: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
    database: Session = Depends(get_db),
) -> JSONResponse:
    credential, organization = principal
    try:
        parsed_resource_id = uuid.UUID(resource_id)
    except ValueError:
        return _scim_error(404, "Resource not found.")
    row = database.execute(
        select(ScimUserMapping, User, OrganizationMember)
        .join(User, User.id == ScimUserMapping.user_id)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == ScimUserMapping.organization_id)
            & (OrganizationMember.user_id == ScimUserMapping.user_id),
        )
        .where(
            ScimUserMapping.id == parsed_resource_id,
            ScimUserMapping.organization_id == organization.id,
            ScimUserMapping.deactivated_at.is_(None),
            OrganizationMember.status.in_(
                [OrganizationMemberStatus.ACTIVE, OrganizationMemberStatus.INACTIVE]
            ),
        )
    ).first()
    if row is None:
        return _scim_error(404, "Resource not found.")
    credential.last_used_at = datetime.now(UTC)
    database.commit()
    return JSONResponse(content=_user_resource(request, *row), media_type="application/scim+json")


def _event(
    database: Session,
    organization: Organization,
    credential: ScimCredential,
    resource_id: uuid.UUID | None,
    action: str,
    outcome: str = "succeeded",
) -> None:
    database.add(
        ScimProvisioningEvent(
            organization_id=organization.id,
            credential_id=credential.id,
            resource_id=resource_id,
            action=action,
            outcome=outcome,
        )
    )


def _payload_email(payload: dict[str, object]) -> tuple[str, str]:
    username = payload.get("userName")
    emails = payload.get("emails")
    if not isinstance(username, str) or not username or len(username) > 320:
        raise ValueError("userName is required.")
    normalized = normalize_email(username)
    try:
        validate_email(normalized, check_deliverability=False)
    except EmailNotValidError:
        raise ValueError("userName must be a valid email.") from None
    if not isinstance(emails, list) or len(emails) != 1 or not isinstance(emails[0], dict):
        raise ValueError("Exactly one work email is required.")
    email = emails[0]
    value = email.get("value")
    if email.get("type") != "work" or not isinstance(value, str) or len(value) > 320:
        raise ValueError("A valid work email is required.")
    work_email = normalize_email(value)
    try:
        validate_email(work_email, check_deliverability=False)
    except EmailNotValidError:
        raise ValueError("A valid work email is required.") from None
    return normalized, work_email


def _resource_row(
    database: Session, organization: Organization, resource_id: str, lock: bool = False
):
    try:
        identifier = uuid.UUID(resource_id)
    except ValueError:
        return None
    statement = (
        select(ScimUserMapping, User, OrganizationMember)
        .join(User, User.id == ScimUserMapping.user_id)
        .join(
            OrganizationMember,
            (OrganizationMember.organization_id == ScimUserMapping.organization_id)
            & (OrganizationMember.user_id == ScimUserMapping.user_id),
        )
        .where(
            ScimUserMapping.id == identifier,
            ScimUserMapping.organization_id == organization.id,
            ScimUserMapping.deactivated_at.is_(None),
        )
    )
    return database.execute(statement.with_for_update() if lock else statement).first()


def _lock_organization(database: Session, organization: Organization) -> Organization:
    locked = database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    if locked is None:
        raise HTTPException(404, "Organization not found.")
    return locked


@scim_router.post("/Users", status_code=201)
async def create_user(
    request: Request,
    principal: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
    database: Session = Depends(get_db),
) -> JSONResponse:
    credential, organization = principal
    try:
        _resource_base()
        payload = await _scim_json_body(request)
        if not isinstance(payload, dict) or set(payload) - {
            "schemas",
            "userName",
            "emails",
            "externalId",
            "active",
        }:
            raise ValueError("Invalid SCIM User payload.")
        if payload.get("active", True) is not True or (
            "schemas" in payload and payload["schemas"] != [SCIM_USER_SCHEMA]
        ):
            raise ValueError("Invalid SCIM User payload.")
        email, work_email = _payload_email(payload)
        external_id = payload.get("externalId")
        if external_id is not None and (
            not isinstance(external_id, str) or not external_id or len(external_id) > 255
        ):
            raise ValueError("Invalid externalId.")
        organization = _lock_organization(database, organization)
        existing_mapping = database.scalar(
            select(ScimUserMapping).where(
                ScimUserMapping.organization_id == organization.id,
                ScimUserMapping.user_name == email,
            )
        )
        if existing_mapping is not None:
            return _scim_error(409, "The SCIM identity already exists.", "uniqueness")
        if database.scalar(select(User.id).where(User.email == email)) is not None:
            return _scim_error(
                409, "An existing account requires owner-approved association.", "uniqueness"
            )
        require_active_seat(database, organization)
        # Initial registration seeds the immutable login email; an alias never
        # selects or adopts an existing global account.
        user = User(email=email, password_hash=hash_password(secrets.token_urlsafe(32)))
        membership = OrganizationMember(
            organization_id=organization.id,
            user=user,
            role=OrganizationMemberRole.MEMBER,
            status=OrganizationMemberStatus.ACTIVE,
        )
        database.add_all([user, membership])
        database.flush()
        mapping = ScimUserMapping(
            organization_id=organization.id,
            user_id=user.id,
            user_name=email,
            work_email=work_email,
            external_id=external_id,
        )
        database.add(mapping)
        database.flush()
        _event(database, organization, credential, mapping.id, "provision")
        database.commit()
        database.refresh(mapping)
        return JSONResponse(
            status_code=201,
            headers={"Location": f"{_resource_base()}/Users/{mapping.id}"},
            content=_user_resource(request, mapping, user, membership),
            media_type="application/scim+json",
        )
    except ValueError as exc:
        database.rollback()
        return _scim_error(400, str(exc), "invalidValue")
    except IntegrityError:
        database.rollback()
        return _scim_error(409, "The SCIM identity already exists.", "uniqueness")
    except SQLAlchemyError:
        database.rollback()
        return _scim_error(500, "SCIM provisioning could not be completed.")


@scim_router.patch("/Users/{resource_id}")
async def patch_user(
    resource_id: str,
    request: Request,
    principal: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
    database: Session = Depends(get_db),
) -> JSONResponse:
    credential, organization = principal
    organization = _lock_organization(database, organization)
    row = _resource_row(database, organization, resource_id, lock=True)
    if row is None:
        return _scim_error(404, "Resource not found.")
    mapping, user, membership = row
    if membership.role in {OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN}:
        return _scim_error(403, "Protected membership cannot be managed.")
    try:
        _resource_base()
        payload = await _scim_json_body(request)
        if not isinstance(payload, dict):
            raise ValueError("Malformed PatchOp.")
        if set(payload) - {"schemas", "Operations"}:
            raise ValueError("Malformed PatchOp.")
        operations = payload.get("Operations")
        if (
            not isinstance(operations, list)
            or not 1 <= len(operations) <= 8
            or (
                "schemas" in payload
                and payload["schemas"] != ["urn:ietf:params:scim:api:messages:2.0:PatchOp"]
            )
        ):
            raise ValueError("Malformed PatchOp.")
        desired_active: bool | None = None
        desired_alias = mapping.user_name
        desired_external_id = mapping.external_id
        desired_work_email = mapping.work_email
        for operation in operations:
            if (
                not isinstance(operation, dict)
                or set(operation) - {"op", "path", "value"}
                or not isinstance(operation.get("op"), str)
            ):
                raise ValueError("Unsupported PatchOp.")
            op = operation["op"].casefold()
            path = operation.get("path")
            if op == "remove" and path == "externalId" and "value" not in operation:
                desired_external_id = None
                continue
            if op not in {"replace", "add"} or "value" not in operation:
                raise ValueError("Unsupported PatchOp.")
            value = operation["value"]
            if path is None and "path" not in operation:
                if not isinstance(value, dict) or not value or len(value) > 3:
                    raise ValueError("Unsupported PatchOp.")
                changes = value
            elif isinstance(path, str):
                changes = {path: value}
            else:
                raise ValueError("Unsupported PatchOp.")
            for field, value in changes.items():
                if field == "userName":
                    if not isinstance(value, str) or not value or len(value) > 320:
                        raise _PatchError("Invalid userName alias.", "invalidValue")
                    desired_alias = normalize_email(value)
                    try:
                        validate_email(desired_alias, check_deliverability=False)
                    except EmailNotValidError:
                        raise _PatchError("Invalid userName alias.", "invalidValue") from None
                elif field == "active" and op == "replace":
                    if not isinstance(value, bool):
                        raise _PatchError("active must be a Boolean.", "invalidValue")
                    desired_active = value
                elif field == "externalId":
                    if not isinstance(value, str) or not value or len(value) > 255:
                        raise _PatchError("Invalid externalId.", "invalidValue")
                    desired_external_id = value
                elif field == 'emails[type eq "work"].value' and op == "replace":
                    if not isinstance(value, str) or not value or len(value) > 320:
                        raise _PatchError("Invalid work email.", "invalidValue")
                    normalized_work_email = normalize_email(value)
                    try:
                        validate_email(normalized_work_email, check_deliverability=False)
                    except EmailNotValidError:
                        raise _PatchError("Invalid work email.", "invalidValue") from None
                    desired_work_email = normalized_work_email
                else:
                    raise ValueError("Unsupported PatchOp.")
        alias_conflict = database.scalar(
            select(ScimUserMapping.id).where(
                ScimUserMapping.organization_id == organization.id,
                func.lower(ScimUserMapping.user_name) == desired_alias,
                ScimUserMapping.id != mapping.id,
            )
        )
        if alias_conflict is not None:
            return _scim_error(409, "The SCIM identity already exists.", "uniqueness")
        if desired_external_id is not None:
            conflict = database.scalar(
                select(ScimUserMapping.id).where(
                    ScimUserMapping.organization_id == organization.id,
                    ScimUserMapping.external_id == desired_external_id,
                    ScimUserMapping.id != mapping.id,
                )
            )
            if conflict is not None:
                return _scim_error(409, "The externalId already exists.", "uniqueness")
        if desired_active is False:
            membership.status = OrganizationMemberStatus.INACTIVE
            action = "deactivation"
        elif desired_active is True and membership.status is not OrganizationMemberStatus.ACTIVE:
            if not user.is_active:
                return _scim_error(409, "Global account is inactive.")
            require_active_seat(database, organization)
            membership.status = OrganizationMemberStatus.ACTIVE
            action = "reactivation"
        else:
            action = "update"
        mapping.external_id = desired_external_id
        mapping.user_name = desired_alias
        mapping.work_email = desired_work_email
        mapping.version += 1
        _event(database, organization, credential, mapping.id, action)
        database.commit()
        return JSONResponse(
            content=_user_resource(request, mapping, user, membership),
            media_type="application/scim+json",
        )
    except ValueError as exc:
        database.rollback()
        return _scim_error(
            400, str(exc), exc.scim_type if isinstance(exc, _PatchError) else "invalidPath"
        )
    except IntegrityError:
        database.rollback()
        return _scim_error(409, "The SCIM identity already exists.", "uniqueness")
    except SQLAlchemyError:
        database.rollback()
        return _scim_error(500, "SCIM update could not be completed.")


@scim_router.delete("/Users/{resource_id}", status_code=204)
def delete_user(
    resource_id: str,
    principal: tuple[ScimCredential, Organization] = Depends(get_scim_principal),
    database: Session = Depends(get_db),
) -> Response:
    credential, organization = principal
    organization = _lock_organization(database, organization)
    row = _resource_row(database, organization, resource_id, lock=True)
    if row is None:
        return _scim_error(404, "Resource not found.")
    mapping, _, membership = row
    if membership.role in {OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN}:
        return _scim_error(403, "Protected membership cannot be managed.")
    try:
        membership.status = OrganizationMemberStatus.INACTIVE
        mapping.deactivated_at = datetime.now(UTC)
        mapping.version += 1
        _event(database, organization, credential, mapping.id, "delete")
        database.commit()
    except SQLAlchemyError:
        database.rollback()
        return _scim_error(500, "SCIM deletion could not be completed.")
    return Response(status_code=204)
