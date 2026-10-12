import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Organization,
    OrganizationEnterpriseQuota,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    ScimConfiguration,
    ScimCredential,
    ScimUserMapping,
    User,
)
from apps.api.app.scim import TOKEN_PREFIX, _hash


def test_schema_describes_supported_email_subattributes(scim_client):
    client, _, token, *_ = scim_client
    response = client.get("/scim/v2/Schemas", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/scim+json")
    body = response.json()
    assert body["totalResults"] == len(body["Resources"]) == 1
    schema = body["Resources"][0]
    assert schema["id"] == "urn:ietf:params:scim:schemas:core:2.0:User"
    attributes = {item["name"]: item for item in schema["attributes"]}
    for name in ("userName", "externalId", "active"):
        assert attributes[name]["multiValued"] is False
    assert attributes["userName"]["uniqueness"] == "server"
    assert attributes["externalId"]["uniqueness"] == "server"
    emails = attributes["emails"]
    assert emails["multiValued"] is True
    children = {item["name"]: item for item in emails["subAttributes"]}
    assert set(children) == {"value", "type", "primary"}
    assert children["value"]["caseExact"] is False
    assert children["type"]["canonicalValues"] == ["work"]
    assert children["primary"]["type"] == "boolean"
    assert children["value"]["mutability"] == "readWrite"
    assert children["type"]["mutability"] == children["primary"]["mutability"] == "immutable"
    assert set(attributes) == {"userName", "externalId", "active", "emails"}


@pytest.fixture
def scim_client(monkeypatch):
    from apps.api.app.scim import settings

    monkeypatch.setattr(settings, "scim_public_base_url", "https://testserver")
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    org_a = Organization(name="A", slug=f"a-{uuid.uuid4().hex}", plan_code="enterprise")
    org_b = Organization(name="B", slug=f"b-{uuid.uuid4().hex}", plan_code="enterprise")
    user_a = User(email=f"reader-{uuid.uuid4().hex}@example.com", password_hash="private-hash")
    user_b = User(email=f"foreign-{uuid.uuid4().hex}@example.com", password_hash="private-hash")
    member_a = OrganizationMember(
        organization=org_a,
        user=user_a,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    member_b = OrganizationMember(
        organization=org_b,
        user=user_b,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    session.add_all([member_a, member_b])
    session.flush()
    session.add_all(
        [
            OrganizationEnterpriseQuota(
                organization_id=organization.id,
                monthly_processing_limit=5_001,
                active_seat_limit=6,
                storage_bytes_limit=11_000_000_000,
            )
            for organization in (org_a, org_b)
        ]
    )
    session.add_all(
        [
            ScimConfiguration(organization_id=org_a.id, enabled=True),
            ScimConfiguration(organization_id=org_b.id, enabled=True),
        ]
    )
    token_a = TOKEN_PREFIX + "a" * 43
    token_b = TOKEN_PREFIX + "b" * 43
    credential_a = ScimCredential(
        organization_id=org_a.id, label="qa-a", secret_hash=_hash(token_a)
    )
    credential_b = ScimCredential(
        organization_id=org_b.id, label="qa-b", secret_hash=_hash(token_b)
    )
    mapping_a = ScimUserMapping(
        organization_id=org_a.id,
        user_id=user_a.id,
        user_name=user_a.email,
        external_id="external-a",
    )
    mapping_b = ScimUserMapping(
        organization_id=org_b.id,
        user_id=user_b.id,
        user_name=user_b.email,
        external_id="external-b",
    )
    session.add_all([credential_a, credential_b, mapping_a, mapping_b])
    session.commit()

    def override_db():
        yield session

    app.dependency_overrides[get_db] = override_db
    try:
        with TestClient(app) as client:
            yield client, session, token_a, token_b, mapping_a, mapping_b, user_a, org_b
    finally:
        app.dependency_overrides.clear()
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_discovery_only_advertises_supported_read_and_filter_features(scim_client):
    client, _, token, *_ = scim_client
    headers = auth(token)
    config = client.get("/scim/v2/ServiceProviderConfig", headers=headers)
    assert config.status_code == 200
    assert config.headers["content-type"].startswith("application/scim+json")
    assert config.json()["filter"] == {"supported": True, "maxResults": 100}
    assert config.json()["patch"]["supported"] is True
    assert config.json()["bulk"]["supported"] is False
    assert config.json()["sort"]["supported"] is False
    assert client.get("/scim/v2/Schemas", headers=headers).json()["totalResults"] == 1
    resource_types = client.get("/scim/v2/ResourceTypes", headers=headers).json()
    assert [item["name"] for item in resource_types["Resources"]] == ["User"]


def test_user_list_get_and_sensitive_data_boundary(scim_client):
    client, _, token, _, mapping, _, user, _ = scim_client
    response = client.get("/scim/v2/Users", headers=auth(token))
    assert response.status_code == 200
    item = response.json()["Resources"][0]
    assert item["id"] == str(mapping.id)
    assert item["userName"] == user.email
    assert item["externalId"] == "external-a"
    assert item["active"] is True
    assert item["emails"] == [{"value": user.email, "type": "work", "primary": True}]
    assert "password_hash" not in str(item)
    assert "auth_version" not in str(item)
    assert "force_password_change" not in str(item)
    assert client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()["id"] == str(
        mapping.id
    )


@pytest.mark.parametrize(
    "filter_value,expected",
    [
        ('userName eq "{email}"', "external-a"),
        ('externalId eq "external-a"', "external-a"),
        ('emails[type eq "work"].value eq "{email}"', "external-a"),
        ('externalId eq "no-match"', None),
    ],
)
def test_supported_filters_and_empty_results(scim_client, filter_value, expected):
    client, _, token, _, _, _, user, _ = scim_client
    response = client.get(
        "/scim/v2/Users",
        params={"filter": filter_value.format(email=user.email.upper())},
        headers=auth(token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["totalResults"] == (1 if expected else 0)
    if expected:
        assert body["Resources"][0]["externalId"] == expected


@pytest.mark.parametrize(
    "filter_value",
    [
        "userName pr",
        'userName co "x"',
        'id eq "x"',
        'userName eq "x" or active eq true',
        'userName eq "x"%27 OR 1=1 --',
    ],
)
def test_malformed_unsupported_and_injection_filters_rejected(scim_client, filter_value):
    client, _, token, *_ = scim_client
    response = client.get("/scim/v2/Users", params={"filter": filter_value}, headers=auth(token))
    assert response.status_code == 400
    assert response.headers["content-type"].startswith("application/scim+json")
    assert response.json()["scimType"] == "invalidFilter"


def test_pagination_is_one_based_bounded_and_deterministic(scim_client):
    client, session, token, _, _, _, _, _ = scim_client
    organization = session.query(Organization).filter(Organization.slug.like("a-%")).one()
    extra_users = [
        User(email=f"page-{i}-{uuid.uuid4().hex}@example.com", password_hash="unused")
        for i in range(3)
    ]
    session.add_all(extra_users)
    session.flush()
    for user in extra_users:
        session.add(
            OrganizationMember(
                organization_id=organization.id,
                user_id=user.id,
                role=OrganizationMemberRole.MEMBER,
                status=OrganizationMemberStatus.ACTIVE,
            )
        )
        session.add(
            ScimUserMapping(organization_id=organization.id, user_id=user.id, user_name=user.email)
        )
    session.commit()
    first = client.get("/scim/v2/Users", headers=auth(token)).json()
    second = client.get(
        "/scim/v2/Users", params={"startIndex": 2, "count": 2}, headers=auth(token)
    ).json()
    assert first["totalResults"] == 4 and first["itemsPerPage"] == 4
    assert second["startIndex"] == 2 and second["itemsPerPage"] == 2 and second["totalResults"] == 4
    assert (
        client.get("/scim/v2/Users", params={"count": 101}, headers=auth(token)).status_code == 400
    )
    assert (
        client.get("/scim/v2/Users", params={"startIndex": 999}, headers=auth(token)).json()[
            "itemsPerPage"
        ]
        == 0
    )


def test_cross_tenant_header_cannot_switch_and_foreign_ids_are_hidden(scim_client):
    client, _, token_a, token_b, _, mapping_b, _, org_b = scim_client
    response = client.get(
        "/scim/v2/Users", headers={**auth(token_a), "X-Organization-ID": str(org_b.id)}
    )
    assert response.status_code == 200 and response.json()["totalResults"] == 1
    foreign = client.get(f"/scim/v2/Users/{mapping_b.id}", headers=auth(token_a))
    assert foreign.status_code == 404
    assert foreign.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert client.get("/scim/v2/Users", headers=auth(token_b)).json()["totalResults"] == 1
    assert client.get("/auth/me", headers=auth(token_a)).status_code == 401
    assert client.get("/external/v1/documents", headers=auth(token_a)).status_code == 401


def test_tombstoned_mapping_and_invalid_credentials_are_not_readable(scim_client):
    client, session, token, _, mapping, _, _, _ = scim_client
    from datetime import UTC, datetime

    mapping.deactivated_at = datetime.now(UTC)
    session.commit()
    assert client.get("/scim/v2/Users", headers=auth(token)).json()["totalResults"] == 0
    missing = client.get("/scim/v2/Users", headers=auth("invx_scim_" + "z" * 43))
    assert missing.status_code == 401
    assert missing.json()["schemas"] == ["urn:ietf:params:scim:api:messages:2.0:Error"]
    assert client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 404


def test_inactive_membership_is_visible_as_inactive_without_tombstone(scim_client):
    client, session, token, _, mapping, _, _, _ = scim_client
    member = session.query(OrganizationMember).filter_by(user_id=mapping.user_id).one()
    member.status = OrganizationMemberStatus.INACTIVE
    session.commit()
    response = client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token))
    assert response.status_code == 200
    assert response.json()["active"] is False
