"""Provider-neutral commercial API access and webhook subscriptions."""

# The API route declarations intentionally keep several contract strings intact.
# ruff: noqa: E501

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import ssl
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpcore
import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, HttpUrl
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.app.database import get_db
from apps.api.app.models import (
    ApiCredential,
    Document,
    Organization,
    OrganizationMemberRole,
    OrganizationSecurityAuditEvent,
    WebhookDelivery,
    WebhookDeliveryStatus,
    WebhookEndpoint,
)
from apps.api.app.plans import (
    CAPABILITY_API_ACCESS,
    CAPABILITY_WEBHOOKS,
    resolve_organization_entitlements,
)
from apps.api.app.rate_limit import RateLimiter, get_rate_limiter
from apps.api.app.settings import settings
from apps.api.app.tenant import TenantContext, TenantPermission, require_tenant_permission

FEATURE_BLOCKED = (
    "This feature is not included in your current plan. Upgrade your plan to access it."
)
MAX_DELIVERY_ATTEMPTS = 5
DELIVERY_TIMEOUT = httpx.Timeout(10.0, connect=3.0)
MAX_RETRY_BACKOFF_SECONDS = 32


class WebhookDestinationError(RuntimeError):
    """A webhook target did not resolve to an allowed public destination."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class _PinnedNetworkBackend(httpcore.NetworkBackend):
    """Connect to a validated IP while httpcore retains the URL hostname for TLS/Host."""

    def __init__(
        self,
        hostname: str,
        port: int,
        address: str,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self.hostname = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        self.port = port
        self.address = address
        self._backend = backend or httpcore.SyncBackend()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        normalized_host = host.encode("idna").decode("ascii").lower().rstrip(".")
        if normalized_host != self.hostname or port != self.port:
            raise httpcore.ConnectError("Unexpected webhook destination.")
        return self._backend.connect_tcp(
            self.address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("Unix sockets are not valid webhook destinations.")


class _CoreResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        yield from self._stream

    def close(self) -> None:
        self._stream.close()


class _PinnedHttpxTransport(httpx.BaseTransport):
    """HTTPX transport that pins TCP to one prevalidated IP and never uses proxies."""

    def __init__(
        self,
        hostname: str,
        port: int,
        address: str,
        backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._pool = httpcore.ConnectionPool(
            ssl_context=ssl.create_default_context(),
            http1=True,
            http2=False,
            max_connections=1,
            max_keepalive_connections=0,
            retries=0,
            network_backend=_PinnedNetworkBackend(hostname, port, address, backend),
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        core_request = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions=request.extensions,
        )
        try:
            response = self._pool.handle_request(core_request)
        except httpcore.TimeoutException as exc:
            raise httpx.TimeoutException("Webhook connection timed out.", request=request) from exc
        except httpcore.NetworkError as exc:
            raise httpx.NetworkError("Webhook connection failed.", request=request) from exc
        except httpcore.ProtocolError as exc:
            raise httpx.ProtocolError(
                "Webhook transport protocol failed.", request=request
            ) from exc
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_CoreResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self) -> None:
        self._pool.close()


def _pinned_webhook_post(
    url: str, destination: tuple[str, int, str], body: str, headers: dict[str, str]
) -> httpx.Response:
    hostname, port, address = destination
    transport = _PinnedHttpxTransport(hostname, port, address)
    with httpx.Client(
        transport=transport,
        timeout=DELIVERY_TIMEOUT,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        return client.post(url, content=body, headers=headers)


def _management_limit(
    request: Request,
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> None:
    limiter.check(
        "credential-management",
        request.client.host if request.client else "unknown",
        30,
        300,
        fail_closed=True,
    )


management_router = APIRouter(
    prefix="/organizations", tags=["commercial access"], dependencies=[Depends(_management_limit)]
)
external_router = APIRouter(prefix="/external/v1", tags=["external API"])


class CredentialCreate(BaseModel):
    label: str = Field(min_length=1, max_length=100)


class CredentialResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class CredentialCreatedResponse(CredentialResponse):
    secret: str


class WebhookCreate(BaseModel):
    url: HttpUrl


class WebhookResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    url: str
    is_active: bool
    created_at: datetime


class WebhookCreatedResponse(WebhookResponse):
    secret: str


class ExternalDocument(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    original_filename: str
    mime_type: str
    file_size: int
    status: str


def _feature_or_403(tenant: TenantContext, capability: str) -> None:
    if tenant.organization is None or not resolve_organization_entitlements(
        tenant.organization
    ).includes(capability):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=FEATURE_BLOCKED)


def _manager_or_403(tenant: TenantContext) -> None:
    if tenant.membership is None or tenant.membership.role not in {
        OrganizationMemberRole.OWNER,
        OrganizationMemberRole.ADMIN,
    }:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to manage this feature.",
        )


def _hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _seal(value: str) -> str:
    key = settings.auth_secret_key.get_secret_value().encode()
    nonce = secrets.token_bytes(16)
    raw = value.encode()
    stream = b"".join(
        hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        for counter in range((len(raw) + 31) // 32)
    )
    ciphertext = bytes(left ^ right for left, right in zip(raw, stream, strict=False))
    mac = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(nonce + mac + ciphertext).decode()


def _decrypt_webhook_secret(endpoint: WebhookEndpoint) -> str:
    try:
        encoded = base64.urlsafe_b64decode(endpoint.secret_ciphertext.encode())
        nonce, mac, ciphertext = encoded[:16], encoded[16:48], encoded[48:]
        key = settings.auth_secret_key.get_secret_value().encode()
        if not hmac.compare_digest(mac, hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()):
            raise ValueError("invalid webhook secret")
        stream = b"".join(
            hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            for counter in range((len(ciphertext) + 31) // 32)
        )
        return bytes(left ^ right for left, right in zip(ciphertext, stream, strict=False)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Webhook signing secret cannot be decrypted.") from exc


def _tenant(tenant: TenantContext) -> tuple[uuid.UUID, Organization]:
    if tenant.organization is None:
        raise HTTPException(status_code=403, detail="An active organization is required.")
    return tenant.organization.id, tenant.organization


@management_router.post(
    "/api-credentials", response_model=CredentialCreatedResponse, status_code=201
)
def create_api_credential(
    payload: CredentialCreate,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> CredentialCreatedResponse:
    _feature_or_403(tenant, CAPABILITY_API_ACCESS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    if database.scalar(
        select(ApiCredential).where(
            ApiCredential.organization_id == organization_id, ApiCredential.label == payload.label
        )
    ):
        raise HTTPException(status_code=409, detail="A credential with this label already exists.")
    secret = "invx_" + secrets.token_urlsafe(32)
    credential = ApiCredential(
        organization_id=organization_id, label=payload.label, secret_hash=_hash_secret(secret)
    )
    database.add(credential)
    database.flush()
    database.add(
        OrganizationSecurityAuditEvent(
            organization_id=organization_id,
            actor_user_id=tenant.membership.user_id if tenant.membership else None,
            action="api_credential_created",
            resource_type="api_credential",
            resource_id=str(credential.id),
            context={"credential_id": str(credential.id)},
        )
    )
    database.commit()
    database.refresh(credential)
    return CredentialCreatedResponse(
        **CredentialResponse.model_validate(credential).model_dump(), secret=secret
    )


@management_router.get("/api-credentials", response_model=list[CredentialResponse])
def list_api_credentials(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[CredentialResponse]:
    _feature_or_403(tenant, CAPABILITY_API_ACCESS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    return [
        CredentialResponse.model_validate(item)
        for item in database.scalars(
            select(ApiCredential)
            .where(ApiCredential.organization_id == organization_id)
            .order_by(ApiCredential.created_at)
        ).all()
    ]


@management_router.delete("/api-credentials/{credential_id}", response_model=CredentialResponse)
def revoke_api_credential(
    credential_id: uuid.UUID,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> CredentialResponse:
    _feature_or_403(tenant, CAPABILITY_API_ACCESS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    credential = database.scalar(
        select(ApiCredential).where(
            ApiCredential.id == credential_id, ApiCredential.organization_id == organization_id
        )
    )
    if credential is None:
        raise HTTPException(status_code=404, detail="API credential not found.")
    credential.revoked_at = datetime.now(UTC)
    database.add(
        OrganizationSecurityAuditEvent(
            organization_id=organization_id,
            actor_user_id=tenant.membership.user_id if tenant.membership else None,
            action="api_credential_revoked",
            resource_type="api_credential",
            resource_id=str(credential.id),
            context={"credential_id": str(credential.id)},
        )
    )
    database.commit()
    database.refresh(credential)
    return CredentialResponse.model_validate(credential)


def get_external_credential(
    api_key: str | None = Header(default=None, alias="X-API-Key"),
    database: Session = Depends(get_db),  # noqa: B008
) -> tuple[ApiCredential, Organization]:
    if not api_key or len(api_key) > 256:
        raise HTTPException(status_code=401, detail="Invalid API credential.")
    credential = database.scalar(
        select(ApiCredential).where(ApiCredential.secret_hash == _hash_secret(api_key))
    )
    if credential is None or credential.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Invalid API credential.")
    organization = credential.organization
    if organization is None or not resolve_organization_entitlements(organization).includes(
        CAPABILITY_API_ACCESS
    ):
        raise HTTPException(status_code=403, detail=FEATURE_BLOCKED)
    credential.last_used_at = datetime.now(UTC)
    database.commit()
    return credential, organization


@external_router.get("/documents", response_model=list[ExternalDocument])
def external_documents(
    request: Request,
    credentials: tuple[ApiCredential, Organization] = Depends(get_external_credential),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> list[ExternalDocument]:
    credential, organization = credentials
    limiter.check("external-api", str(credential.id), 60, 60)
    return [
        ExternalDocument.model_validate(item)
        for item in database.scalars(
            select(Document)
            .where(Document.organization_id == organization.id)
            .order_by(Document.created_at.desc())
            .limit(100)
        ).all()
    ]


def _validate_webhook_url(url: str) -> tuple[str, int, str]:
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        port = parsed.port or 443
    except ValueError as exc:
        raise WebhookDestinationError("invalid_url") from exc
    if parsed.scheme != "https" or parsed.username or parsed.password or not hostname:
        raise WebhookDestinationError("invalid_url")
    try:
        answers = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise WebhookDestinationError("dns_failure") from exc
    if not answers:
        raise WebhookDestinationError("dns_failure")

    addresses: set[str] = set()
    for answer in answers:
        try:
            address = ipaddress.ip_address(answer[4][0])
        except ValueError as exc:
            raise WebhookDestinationError("unsafe_address") from exc
        mapped_address = address.ipv4_mapped if isinstance(address, ipaddress.IPv6Address) else None
        effective_address = mapped_address or address
        if (
            not effective_address.is_global
            or effective_address.is_private
            or effective_address.is_loopback
            or effective_address.is_link_local
            or effective_address.is_multicast
            or effective_address.is_unspecified
            or effective_address.is_reserved
        ):
            raise WebhookDestinationError("unsafe_address")
        addresses.add(address.compressed)
    if not addresses:
        raise WebhookDestinationError("dns_failure")

    # The transport connects to this validated IP; the request URL retains the hostname.
    return hostname, port, sorted(addresses)[0]


@management_router.post("/webhooks", response_model=WebhookCreatedResponse, status_code=201)
def create_webhook(
    payload: WebhookCreate,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> WebhookResponse:
    _feature_or_403(tenant, CAPABILITY_WEBHOOKS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    url = str(payload.url)
    try:
        _validate_webhook_url(url)
    except WebhookDestinationError as exc:
        detail = (
            "Webhook host cannot be resolved."
            if exc.reason == "dns_failure"
            else "Webhook URL must use HTTPS and resolve only to public addresses."
        )
        raise HTTPException(status_code=422, detail=detail) from exc
    secret = "whsec_" + secrets.token_urlsafe(32)
    endpoint = WebhookEndpoint(
        organization_id=organization_id,
        url=url,
        secret_hash=_hash_secret(secret),
        secret_ciphertext=_seal(secret),
    )
    database.add(endpoint)
    database.commit()
    database.refresh(endpoint)
    return WebhookCreatedResponse(
        **WebhookResponse.model_validate(endpoint).model_dump(), secret=secret
    )


@management_router.get("/webhooks", response_model=list[WebhookResponse])
def list_webhooks(
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> list[WebhookResponse]:
    _feature_or_403(tenant, CAPABILITY_WEBHOOKS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    return [
        WebhookResponse.model_validate(item)
        for item in database.scalars(
            select(WebhookEndpoint)
            .where(WebhookEndpoint.organization_id == organization_id)
            .order_by(WebhookEndpoint.created_at)
        ).all()
    ]


@management_router.patch("/webhooks/{endpoint_id}", response_model=WebhookResponse)
def set_webhook_state(
    endpoint_id: uuid.UUID,
    enabled: bool,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> WebhookResponse:
    _feature_or_403(tenant, CAPABILITY_WEBHOOKS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    endpoint = database.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id, WebhookEndpoint.organization_id == organization_id
        )
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found.")
    endpoint.is_active = enabled
    database.commit()
    database.refresh(endpoint)
    return WebhookResponse.model_validate(endpoint)


@management_router.delete("/webhooks/{endpoint_id}", status_code=204)
def delete_webhook(
    endpoint_id: uuid.UUID,
    tenant: TenantContext = Depends(require_tenant_permission(TenantPermission.READ)),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> None:
    _feature_or_403(tenant, CAPABILITY_WEBHOOKS)
    _manager_or_403(tenant)
    organization_id, _ = _tenant(tenant)
    endpoint = database.scalar(
        select(WebhookEndpoint).where(
            WebhookEndpoint.id == endpoint_id, WebhookEndpoint.organization_id == organization_id
        )
    )
    if endpoint is None:
        raise HTTPException(status_code=404, detail="Webhook endpoint not found.")
    database.delete(endpoint)
    database.commit()


def deliver_webhook(
    database: Session, endpoint: WebhookEndpoint, event_type: str, payload: dict
) -> WebhookDelivery:
    """Deliver one durable logical event; callers may schedule retries externally."""
    event_id = uuid.uuid4()
    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        organization_id=endpoint.organization_id,
        event_id=event_id,
        event_type=event_type,
        payload=payload,
    )
    database.add(delivery)
    database.flush()
    attempt_webhook_delivery(database, delivery)
    return delivery


def attempt_webhook_delivery(database: Session, delivery: WebhookDelivery) -> None:
    """Attempt a persisted delivery again without changing its event identity."""
    endpoint = database.get(WebhookEndpoint, delivery.endpoint_id)
    if endpoint is None or not endpoint.is_active:
        delivery.status = WebhookDeliveryStatus.FAILED
        delivery.last_error = "Webhook endpoint is unavailable."
        database.commit()
        return
    delivery.attempt_count += 1
    try:
        destination = _validate_webhook_url(endpoint.url)
        body = json.dumps(
            {
                "id": str(delivery.event_id),
                "type": delivery.event_type,
                "created_at": datetime.now(UTC).isoformat(),
                "data": delivery.payload,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        timestamp = str(int(datetime.now(UTC).timestamp()))
        signature = hmac.new(
            _decrypt_webhook_secret(endpoint).encode(),
            f"{timestamp}.{body}".encode(),
            hashlib.sha256,
        ).hexdigest()
        response = _pinned_webhook_post(
            endpoint.url,
            destination,
            body,
            {
                "Content-Type": "application/json",
                "X-Invoxa-Event-Id": str(delivery.event_id),
                "X-Invoxa-Timestamp": timestamp,
                "X-Invoxa-Signature": f"t={timestamp},v1={signature}",
            },
        )
        if 200 <= response.status_code < 300:
            delivery.status = WebhookDeliveryStatus.SUCCEEDED
            delivery.delivered_at = datetime.now(UTC)
        else:
            raise RuntimeError("remote_http_error")
    except WebhookDestinationError as exc:
        failure_code = exc.reason
    except (httpx.HTTPError, RuntimeError):
        failure_code = "delivery_failed"
    else:
        database.commit()
        return

    if delivery.status != WebhookDeliveryStatus.SUCCEEDED:
        delivery.status = (
            WebhookDeliveryStatus.RETRYING
            if delivery.attempt_count < MAX_DELIVERY_ATTEMPTS
            else WebhookDeliveryStatus.FAILED
        )
        delivery.last_error = failure_code
        delivery.next_attempt_at = (
            datetime.now(UTC)
            + timedelta(seconds=min(2**delivery.attempt_count, MAX_RETRY_BACKOFF_SECONDS))
            if delivery.status == WebhookDeliveryStatus.RETRYING
            else None
        )
    database.commit()


def retry_due_webhook_deliveries(database: Session, batch_size: int = 20) -> int:
    """Claim and retry a bounded batch without duplicating logical deliveries."""
    now = datetime.now(UTC)
    deliveries = database.scalars(
        select(WebhookDelivery)
        .where(
            WebhookDelivery.status == WebhookDeliveryStatus.RETRYING,
            WebhookDelivery.next_attempt_at.is_not(None),
            WebhookDelivery.next_attempt_at <= now,
        )
        .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.created_at)
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for delivery in deliveries:
        attempt_webhook_delivery(database, delivery)
    return len(deliveries)


def emit_document_event(
    database: Session, organization_id: uuid.UUID, event_type: str, payload: dict
) -> None:
    """Deliver a durable document lifecycle event to active tenant endpoints."""
    endpoints = database.scalars(
        select(WebhookEndpoint).where(
            WebhookEndpoint.organization_id == organization_id,
            WebhookEndpoint.is_active.is_(True),
        )
    ).all()
    for endpoint in endpoints:
        deliver_webhook(database, endpoint, event_type, payload)
