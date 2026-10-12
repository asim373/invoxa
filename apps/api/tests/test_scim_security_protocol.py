"""Adversarial checks for the supported SCIM V1 wire contract."""

# ruff: noqa: F811  # imported pytest fixture is injected by name

import socket
import threading
import time
import uuid

import httpx
import pytest
import redis
import uvicorn
from sqlalchemy import select

from apps.api.app import scim
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    ScimConfiguration,
    ScimCredential,
    User,
)
from apps.api.app.rate_limit import RateLimiter
from apps.api.app.settings import settings
from apps.api.tests.test_scim_read import auth, scim_client  # noqa: F401


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Basic abc",
        "Bearer ",
        "Bearer wrong",
        "Bearer invx_scim_" + "z" * 43,
        "Bearer invx_scim_" + "x" * 300,
    ],
)
def test_invalid_credentials_are_controlled_and_non_disclosing(scim_client, header):
    client, _, token, *_ = scim_client
    headers = {} if header is None else {"Authorization": header}
    response = client.get("/scim/v2/Users", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/scim+json")
    body = response.json()
    assert body["schemas"] == [scim.SCIM_ERROR_SCHEMA]
    assert body["status"] == "401"
    assert token not in str(body) and "sql" not in str(body).casefold()


def test_disabled_downgraded_inactive_and_revoked_credentials(scim_client):
    client, session, token, _, _, _, _, other_org = scim_client
    organization = session.scalar(select(Organization).where(Organization.id != other_org.id))
    assert organization is not None
    configuration = session.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    credential = session.scalar(
        select(ScimCredential).where(ScimCredential.organization_id == organization.id)
    )
    assert configuration is not None and credential is not None
    configuration.enabled = False
    session.commit()
    assert client.get("/scim/v2/Users", headers=auth(token)).status_code == 403
    configuration.enabled = True
    organization.plan_code = "standard"
    session.commit()
    assert client.get("/scim/v2/Users", headers=auth(token)).status_code == 403
    organization.plan_code = "enterprise"
    original_status = organization.status
    organization.status = type(original_status).INACTIVE
    session.commit()
    assert client.get("/scim/v2/Users", headers=auth(token)).status_code == 403
    organization.status = original_status
    credential.revoked_at = scim.datetime.now(scim.UTC)
    session.commit()
    assert client.get("/scim/v2/Users", headers=auth(token)).status_code == 401


@pytest.mark.parametrize(
    "query",
    [
        {"filter": 'userName co "x"'},
        {"filter": 'userName eq "x" or 1=1'},
        {"filter": 'externalId eq "\' OR 1=1 --"'},
        {"filter": 'userName eq "x" ' * 100},
        {"count": "-1"},
        {"count": "101"},
        {"count": "999999999999999999999"},
        {"count": "not-an-int"},
        {"startIndex": "0"},
        {"startIndex": "-1"},
    ],
)
def test_filter_and_pagination_bounds(scim_client, query):
    client, _, token, *_ = scim_client
    response = client.get("/scim/v2/Users", params=query, headers=auth(token))
    if query.get("filter") == 'externalId eq "\' OR 1=1 --"':
        assert response.status_code == 200 and response.json()["totalResults"] == 0
    else:
        assert response.status_code == 400
        assert response.json()["scimType"] in {"invalidFilter", "invalidValue"}


def test_scim_body_bounds_content_type_and_malformed_json(scim_client):
    client, session, token, *_ = scim_client
    headers = auth(token)
    for content, content_type, expected in [
        (b"", "application/scim+json", 400),
        (b"{", "application/scim+json", 400),
        (b"\xff", "application/scim+json", 400),
        (b"{}", "text/plain", 415),
        (b" " * 65537, "application/scim+json", 413),
    ]:
        response = client.post(
            "/scim/v2/Users",
            content=content,
            headers={**headers, "Content-Type": content_type},
        )
        assert response.status_code == expected
        assert response.json()["schemas"] == [scim.SCIM_ERROR_SCHEMA]
    assert session.scalar(select(User).where(User.email == "bad@example.com")) is None


def test_malformed_patch_operator_returns_scim_error(scim_client):
    client, _, token, _, mapping, *_ = scim_client
    response = client.patch(
        f"/scim/v2/Users/{mapping.id}",
        json={"Operations": [{"op": 42, "path": "active", "value": False}]},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert response.json()["schemas"] == [scim.SCIM_ERROR_SCHEMA]


def test_unsupported_method_is_controlled(scim_client):
    client, _, token, *_ = scim_client
    response = client.put("/scim/v2/Users", headers=auth(token), json={})
    assert response.status_code == 405
    assert response.headers["content-type"].startswith("application/scim+json")
    assert response.json()["schemas"] == [scim.SCIM_ERROR_SCHEMA]


def test_workspace_delete_preserves_other_tenant_membership_and_global_security(scim_client):
    client, session, token, _, mapping, _, user, other_org = scim_client
    other_member = OrganizationMember(
        organization_id=other_org.id,
        user_id=user.id,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    session.add(other_member)
    session.commit()
    before = (user.is_active, user.auth_version, user.password_hash, user.force_password_change)
    assert client.delete(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 204
    session.refresh(other_member)
    session.refresh(user)
    assert other_member.status is OrganizationMemberStatus.ACTIVE
    assert (
        user.is_active,
        user.auth_version,
        user.password_hash,
        user.force_password_change,
    ) == before


def test_discovery_mutability_matches_supported_patch(scim_client):
    client, _, token, *_ = scim_client
    config = client.get("/scim/v2/ServiceProviderConfig", headers=auth(token)).json()
    assert config["patch"]["supported"] is True
    for field in ("bulk", "sort", "changePassword", "etag"):
        assert config[field]["supported"] is False
    schema = client.get("/scim/v2/Schemas", headers=auth(token)).json()["Resources"][0]
    mutability = {item["name"]: item["mutability"] for item in schema["attributes"]}
    assert mutability == {
        "userName": "readWrite",
        "externalId": "readWrite",
        "active": "readWrite",
        "emails": "readWrite",
    }


def test_scim_rate_limit_is_credential_scoped_and_fails_closed(scim_client, monkeypatch):
    client, _, token, *_ = scim_client
    monkeypatch.setattr(settings, "rate_limit_enabled", True)

    class Counter:
        count = 0

        def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
            self.count += 1
            return self.count

    counter = Counter()
    app.dependency_overrides[scim.get_rate_limiter] = lambda: RateLimiter(counter)
    try:
        counter.count = 300
        blocked_peer = client.get("/scim/v2/Users", headers=auth("invalid"))
        assert blocked_peer.status_code == 429
        assert blocked_peer.headers["Retry-After"] == "60"
        counter.count = 120
        limited = client.get("/scim/v2/Users", headers=auth(token))
        assert limited.status_code == 429
        assert limited.headers["Retry-After"] == "60"
        assert limited.json()["schemas"] == [scim.SCIM_ERROR_SCHEMA]

        class Outage:
            def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
                raise redis.RedisError("private backend detail")

        app.dependency_overrides[scim.get_rate_limiter] = lambda: RateLimiter(Outage())
        unavailable = client.get("/scim/v2/Users", headers=auth(token))
        assert unavailable.status_code == 503
        assert "private backend detail" not in str(unavailable.json())
    finally:
        app.dependency_overrides.pop(scim.get_rate_limiter, None)


def test_real_http_scim_lifecycle_and_tenant_isolation(scim_client):
    _, _, token, _, mapping, foreign, _, other_org = scim_client
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as http:
            headers = auth(token)
            for route in ("ServiceProviderConfig", "Schemas", "ResourceTypes"):
                result = http.get(f"/scim/v2/{route}", headers=headers)
                assert result.status_code == 200
                assert result.headers["content-type"].startswith("application/scim+json")
            assert http.get("/scim/v2/Users", headers=headers).json()["totalResults"] == 1
            assert (
                http.get(
                    "/scim/v2/Users",
                    params={"filter": 'userName eq "nobody@example.com"'},
                    headers=headers,
                ).json()["totalResults"]
                == 0
            )
            email = f"wire-{uuid.uuid4().hex}@example.com"
            payload = {
                "schemas": [scim.SCIM_USER_SCHEMA],
                "userName": email,
                "emails": [{"type": "work", "value": email}],
                "externalId": "wire-id",
            }
            created = http.post("/scim/v2/Users", json=payload, headers=headers)
            assert created.status_code == 201
            from urllib.parse import urlsplit

            absolute_location = created.headers["Location"]
            location = urlsplit(absolute_location).path
            resource = created.json()
            assert resource["meta"]["location"].endswith(location)
            assert resource["meta"]["location"] == absolute_location
            assert resource["id"] != str(mapping.user_id)
            assert "password" not in str(resource).casefold()
            assert http.get(location, headers=headers).json()["id"] == resource["id"]
            conflicting = {**payload, "userName": f"other-{uuid.uuid4().hex}@example.com"}
            conflicting["emails"] = [{"type": "work", "value": conflicting["userName"]}]
            assert http.post("/scim/v2/Users", json=conflicting, headers=headers).status_code == 409
            assert (
                http.patch(
                    location,
                    headers=headers,
                    json={"Operations": [{"op": "replace", "path": "active", "value": False}]},
                ).json()["active"]
                is False
            )
            assert http.delete(location, headers=headers).status_code == 204
            assert http.get(location, headers=headers).status_code == 404
            assert http.get(f"/scim/v2/Users/{foreign.id}", headers=headers).status_code == 404
            assert (
                http.get(
                    "/scim/v2/Users", headers={**headers, "X-Organization-ID": str(other_org.id)}
                ).json()["totalResults"]
                == 1
            )
            assert http.get("/scim/v2/Users", headers=auth("invalid")).status_code == 401
            assert (
                http.get(
                    "/scim/v2/Users", params={"filter": 'userName co "x"'}, headers=headers
                ).status_code
                == 400
            )
            duplicate = http.post("/scim/v2/Users", json=payload, headers=headers)
            assert duplicate.status_code == 409
            assert duplicate.json()["scimType"] == "uniqueness"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive()
