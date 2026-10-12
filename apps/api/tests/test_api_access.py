import hashlib
import hmac
import json
import socket
import ssl
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app import api_access
from apps.api.app.api_access import (
    WebhookDestinationError,
    _hash_secret,
    _PinnedHttpxTransport,
    _seal,
    _validate_webhook_url,
    deliver_webhook,
    retry_due_webhook_deliveries,
)
from apps.api.app.auth import create_access_token
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    ApiCredential,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    User,
    WebhookDeliveryStatus,
    WebhookEndpoint,
)
from apps.api.app.plans import DEFAULT_PLAN_CODE, PROFESSIONAL_PLAN_CODE


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        app.dependency_overrides[get_db] = lambda: session
        yield session
    app.dependency_overrides.clear()
    Base.metadata.drop_all(engine)
    engine.dispose()


def seed(database: Session, plan_code: str) -> tuple[User, Organization]:
    user = User(
        id=uuid.uuid4(), email=f"owner-{uuid.uuid4().hex}@example.com", password_hash="unused"
    )
    organization = Organization(name="Acme", slug=f"acme-{uuid.uuid4().hex}", plan_code=plan_code)
    member = OrganizationMember(
        organization=organization,
        user=user,
        role=OrganizationMemberRole.OWNER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add_all([user, organization, member])
    database.commit()
    return user, organization


def auth_headers(user: User, organization: Organization) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {create_access_token(user.id)}",
        "X-Organization-ID": str(organization.id),
    }


def test_standard_is_blocked_and_professional_secret_is_one_time(database: Session) -> None:
    standard_user, standard = seed(database, DEFAULT_PLAN_CODE)
    client = TestClient(app)
    blocked = client.post(
        "/organizations/api-credentials",
        json={"label": "standard"},
        headers=auth_headers(standard_user, standard),
    )
    assert blocked.status_code == 403
    professional_user, professional = seed(database, PROFESSIONAL_PLAN_CODE)
    created = client.post(
        "/organizations/api-credentials",
        json={"label": "primary"},
        headers=auth_headers(professional_user, professional),
    )
    assert created.status_code == 201
    secret = created.json()["secret"]
    assert secret.startswith("invx_")
    record = database.scalar(
        select(ApiCredential).where(ApiCredential.organization_id == professional.id)
    )
    assert record is not None
    assert secret not in record.secret_hash
    listed = client.get(
        "/organizations/api-credentials", headers=auth_headers(professional_user, professional)
    )
    assert listed.status_code == 200
    assert "secret" not in listed.json()[0]
    external = client.get("/external/v1/documents", headers={"X-API-Key": secret})
    assert external.status_code == 200
    client.delete(
        f"/organizations/api-credentials/{record.id}",
        headers=auth_headers(professional_user, professional),
    )
    assert client.get("/external/v1/documents", headers={"X-API-Key": secret}).status_code == 401


def test_webhook_internal_url_is_rejected() -> None:
    with pytest.raises(WebhookDestinationError):
        _validate_webhook_url("https://127.0.0.1/receiver")


def _dns_answer(address: str, port: int = 443) -> tuple:
    if ":" in address:
        family = socket.AF_INET6
        sockaddr = (address, port, 0, 0)
    else:
        family = socket.AF_INET
        sockaddr = (address, port)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr


def test_webhook_public_dns_destination_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *_args, **_kwargs: [_dns_answer("93.184.216.34")]
    )
    assert _validate_webhook_url("https://hooks.example.test/receiver") == (
        "hooks.example.test",
        443,
        "93.184.216.34",
    )


@pytest.mark.parametrize(
    "address",
    ["127.0.0.1", "10.1.2.3", "169.254.10.20", "224.0.0.1", "240.0.0.1", "::1"],
)
def test_webhook_unsafe_dns_destinations_are_rejected(
    monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_args, **_kwargs: [_dns_answer(address)])
    with pytest.raises(WebhookDestinationError):
        _validate_webhook_url("https://hooks.example.test/receiver")


def test_webhook_mixed_public_and_private_dns_answers_are_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            _dns_answer("93.184.216.34"),
            _dns_answer("10.0.0.8"),
        ],
    )
    with pytest.raises(WebhookDestinationError):
        _validate_webhook_url("https://hooks.example.test/receiver")


class _RecordingTlsStream(httpcore.NetworkStream):
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.written = bytearray()
        self.server_hostname: str | None = None
        self.ssl_context: ssl.SSLContext | None = None

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk = self.response[:max_bytes]
        self.response = self.response[max_bytes:]
        return chunk

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written.extend(buffer)

    def close(self) -> None:
        return None

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        self.ssl_context = ssl_context
        self.server_hostname = server_hostname
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class _RecordingNetworkBackend(httpcore.NetworkBackend):
    def __init__(self, stream: _RecordingTlsStream) -> None:
        self.stream = stream
        self.connected_to: list[tuple[str, int]] = []

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        self.connected_to.append((host, port))
        return self.stream

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: Any = None
    ) -> httpcore.NetworkStream:
        raise AssertionError("Webhook transport must not use Unix sockets.")


def test_pinned_transport_preserves_tls_host_disables_redirects_and_ignores_proxies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = _RecordingTlsStream(
        b"HTTP/1.1 302 Found\r\nContent-Length: 0\r\nLocation: https://127.0.0.1/\r\n\r\n"
    )
    backend = _RecordingNetworkBackend(stream)
    transport = _PinnedHttpxTransport("hooks.example.test", 443, "93.184.216.34", backend)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("NO_PROXY", "")

    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        response = client.post("https://hooks.example.test/receiver", content=b"{}")

    assert response.status_code == 302
    assert backend.connected_to == [("93.184.216.34", 443)]
    assert stream.server_hostname == "hooks.example.test"
    assert stream.ssl_context is not None
    assert stream.ssl_context.check_hostname is True
    assert stream.ssl_context.verify_mode == ssl.CERT_REQUIRED
    assert b"host: hooks.example.test" in bytes(stream.written).lower()


def test_dns_rebinding_cannot_change_the_pinned_connection_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = iter([[_dns_answer("93.184.216.34")], [_dns_answer("10.0.0.8")]])
    lookup_count = 0

    def rotating_dns(*_args, **_kwargs):
        nonlocal lookup_count
        lookup_count += 1
        return next(resolutions)

    monkeypatch.setattr(socket, "getaddrinfo", rotating_dns)
    destination = _validate_webhook_url("https://hooks.example.test/receiver")
    stream = _RecordingTlsStream(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
    backend = _RecordingNetworkBackend(stream)
    transport = _PinnedHttpxTransport(*destination, backend)
    with httpx.Client(transport=transport, trust_env=False, follow_redirects=False) as client:
        response = client.post("https://hooks.example.test/receiver", content=b"{}")

    assert response.status_code == 204
    assert lookup_count == 1
    assert backend.connected_to == [("93.184.216.34", 443)]
    assert stream.server_hostname == "hooks.example.test"


def test_webhook_secret_envelope_fits_postgresql_column_contract() -> None:
    assert 128 < len(_seal("whsec_" + "x" * 43)) <= 255


def test_webhook_retry_preserves_the_logical_event_id(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, organization = seed(database, PROFESSIONAL_PLAN_CODE)
    secret = "whsec_" + "x" * 43
    endpoint = WebhookEndpoint(
        organization_id=organization.id,
        url="https://example.com/hook",
        secret_hash=_hash_secret(secret),
        secret_ciphertext=_seal(secret),
    )
    database.add(endpoint)
    database.commit()
    monkeypatch.setattr(
        api_access, "_validate_webhook_url", lambda _url: ("example.com", 443, "93.184.216.34")
    )
    monkeypatch.setattr(
        api_access,
        "_pinned_webhook_post",
        lambda url, *_args: httpx.Response(500, request=httpx.Request("POST", url)),
    )
    delivery = deliver_webhook(
        database, endpoint, "document.processing.completed", {"status": "completed"}
    )
    assert delivery.status == WebhookDeliveryStatus.RETRYING
    event_id = delivery.event_id
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    database.commit()
    assert retry_due_webhook_deliveries(database) == 1
    database.refresh(delivery)
    assert delivery.event_id == event_id
    assert delivery.attempt_count == 2
    for expected_attempt in range(3, 6):
        delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        database.commit()
        assert retry_due_webhook_deliveries(database) == 1
        database.refresh(delivery)
        assert delivery.attempt_count == expected_attempt
        assert delivery.event_id == event_id
    assert delivery.status == WebhookDeliveryStatus.FAILED
    assert delivery.next_attempt_at is None


def test_webhook_dns_failure_is_persisted_as_bounded_retry_without_secret_leak(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, organization = seed(database, PROFESSIONAL_PLAN_CODE)
    secret = "whsec-do-not-log-this"
    endpoint = WebhookEndpoint(
        organization_id=organization.id,
        url="https://private-looking.example/hook",
        secret_hash=_hash_secret(secret),
        secret_ciphertext=_seal(secret),
    )
    database.add(endpoint)
    database.commit()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("sensitive DNS detail")),
    )

    before_attempt = datetime.now(UTC)
    delivery = deliver_webhook(database, endpoint, "document.processing.completed", {"x": 1})

    assert delivery.status == WebhookDeliveryStatus.RETRYING
    assert delivery.attempt_count == 1
    assert delivery.next_attempt_at is not None
    scheduled_at = delivery.next_attempt_at
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    assert 1.0 <= (scheduled_at - before_attempt).total_seconds() <= 3.0
    assert delivery.last_error == "dns_failure"
    assert secret not in str(delivery.last_error)
    assert endpoint.url not in str(delivery.last_error)
    assert "sensitive DNS detail" not in str(delivery.last_error)


def test_webhook_management_cannot_mutate_another_organizations_endpoint(
    database: Session,
) -> None:
    first_user, first_org = seed(database, PROFESSIONAL_PLAN_CODE)
    second_user, second_org = seed(database, PROFESSIONAL_PLAN_CODE)
    endpoint = WebhookEndpoint(
        organization_id=first_org.id,
        url="https://hooks.example.test/hook",
        secret_hash=_hash_secret("whsec-test"),
        secret_ciphertext=_seal("whsec-test"),
    )
    database.add(endpoint)
    database.commit()
    client = TestClient(app)

    foreign_patch = client.patch(
        f"/organizations/webhooks/{endpoint.id}?enabled=false",
        headers=auth_headers(second_user, second_org),
    )
    foreign_delete = client.delete(
        f"/organizations/webhooks/{endpoint.id}",
        headers=auth_headers(second_user, second_org),
    )

    assert foreign_patch.status_code == 404
    assert foreign_delete.status_code == 404
    database.refresh(endpoint)
    assert endpoint.is_active is True
    assert (
        client.get("/organizations/webhooks", headers=auth_headers(second_user, second_org)).json()
        == []
    )
    assert client.get(
        "/organizations/webhooks", headers=auth_headers(first_user, first_org)
    ).json()[0]["id"] == str(endpoint.id)


def test_webhook_dns_failure_persists_terminal_status_after_maximum_attempts(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, organization = seed(database, PROFESSIONAL_PLAN_CODE)
    endpoint = WebhookEndpoint(
        organization_id=organization.id,
        url="https://hooks.example.test/hook",
        secret_hash=_hash_secret("whsec-test"),
        secret_ciphertext=_seal("whsec-test"),
    )
    database.add(endpoint)
    database.flush()
    from apps.api.app.models import WebhookDelivery

    delivery = WebhookDelivery(
        endpoint_id=endpoint.id,
        organization_id=organization.id,
        event_id=uuid.uuid4(),
        event_type="document.processing.completed",
        payload={"status": "completed"},
        status=WebhookDeliveryStatus.RETRYING,
        attempt_count=4,
        next_attempt_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    database.add(delivery)
    database.commit()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(socket.gaierror("unavailable")),
    )

    assert retry_due_webhook_deliveries(database) == 1
    database.refresh(delivery)
    assert delivery.attempt_count == 5
    assert delivery.status == WebhookDeliveryStatus.FAILED
    assert delivery.next_attempt_at is None
    assert delivery.last_error == "dns_failure"


def test_webhook_retry_batch_continues_after_destination_failure(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.api.app.models import WebhookDelivery

    _, organization = seed(database, PROFESSIONAL_PLAN_CODE)
    due = datetime.now(UTC) - timedelta(seconds=1)
    endpoints = [
        WebhookEndpoint(
            organization_id=organization.id,
            url=f"https://{host}.example.test/hook",
            secret_hash=_hash_secret("whsec-test"),
            secret_ciphertext=_seal("whsec-test"),
        )
        for host in ("blocked", "valid")
    ]
    database.add_all(endpoints)
    database.flush()
    deliveries = [
        WebhookDelivery(
            endpoint_id=endpoint.id,
            organization_id=organization.id,
            event_id=uuid.uuid4(),
            event_type="document.processing.completed",
            payload={"status": "completed"},
            status=WebhookDeliveryStatus.RETRYING,
            attempt_count=1,
            next_attempt_at=due,
        )
        for endpoint in endpoints
    ]
    database.add_all(deliveries)
    database.commit()

    def resolve(host: str, *_args, **_kwargs):
        if host.startswith("blocked."):
            raise socket.gaierror("not logged")
        return [_dns_answer("93.184.216.34")]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    monkeypatch.setattr(
        api_access,
        "_pinned_webhook_post",
        lambda url, *_args: httpx.Response(204, request=httpx.Request("POST", url)),
    )

    assert retry_due_webhook_deliveries(database) == 2
    database.refresh(deliveries[0])
    database.refresh(deliveries[1])
    assert deliveries[0].attempt_count == 2
    assert deliveries[0].status == WebhookDeliveryStatus.RETRYING
    assert deliveries[0].next_attempt_at is not None
    assert deliveries[1].attempt_count == 2
    assert deliveries[1].status == WebhookDeliveryStatus.SUCCEEDED


def test_webhook_delivery_hmac_remains_valid_and_event_id_stable(
    database: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, organization = seed(database, PROFESSIONAL_PLAN_CODE)
    secret = "whsec-preserve-hmac-contract"
    endpoint = WebhookEndpoint(
        organization_id=organization.id,
        url="https://hooks.example.test/hook",
        secret_hash=_hash_secret(secret),
        secret_ciphertext=_seal(secret),
    )
    database.add(endpoint)
    database.commit()
    monkeypatch.setattr(
        api_access,
        "_validate_webhook_url",
        lambda _url: ("hooks.example.test", 443, "93.184.216.34"),
    )
    captured: dict[str, str] = {}

    def accept(url: str, _destination, body: str, headers: dict[str, str]) -> httpx.Response:
        captured.update(headers)
        captured["body"] = body
        return httpx.Response(204, request=httpx.Request("POST", url))

    monkeypatch.setattr(api_access, "_pinned_webhook_post", accept)
    delivery = deliver_webhook(
        database, endpoint, "document.processing.completed", {"status": "completed"}
    )
    signature = captured["X-Invoxa-Signature"].removeprefix("t=").split(",v1=")
    expected = hmac.new(
        secret.encode(), f"{signature[0]}.{captured['body']}".encode(), hashlib.sha256
    ).hexdigest()

    assert signature == [captured["X-Invoxa-Timestamp"], expected]
    assert json.loads(captured["body"])["id"] == str(delivery.event_id)
    assert captured["X-Invoxa-Event-Id"] == str(delivery.event_id)
    assert delivery.status == WebhookDeliveryStatus.SUCCEEDED
