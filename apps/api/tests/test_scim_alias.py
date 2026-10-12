"""Tenant-scoped alias and trusted public resource URL security regressions."""

# ruff: noqa: F811
import uuid

import pytest
from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict
from sqlalchemy import select

from apps.api.app.models import (
    OrganizationMember,
    OrganizationSamlConfiguration,
    SamlIdentity,
    User,
)
from apps.api.app.scim import settings
from apps.api.app.settings import Settings
from apps.api.tests.test_scim_interop import patch
from apps.api.tests.test_scim_lifecycle import post
from apps.api.tests.test_scim_read import auth, scim_client  # noqa: F401


class _SettingsWithoutEnv(Settings):
    model_config = SettingsConfigDict(env_file=None, extra="ignore")


def test_alias_rename_preserves_login_saml_binding_and_stable_resource(scim_client):
    client, db, token, foreign_token, mapping, foreign, user, _ = scim_client
    old_alias = mapping.user_name
    canonical = (user.email, user.password_hash, user.auth_version, user.force_password_change)
    config = OrganizationSamlConfiguration(
        organization_id=mapping.organization_id,
        enabled=True,
        idp_entity_id="https://idp.example.test",
        idp_sso_url="https://idp.example.test/sso",
        signing_certificates=["public-qa-certificate"],
    )
    db.add(config)
    db.flush()
    identity = SamlIdentity(
        configuration_id=config.id, user_id=user.id, subject="stable-qa-subject"
    )
    db.add(identity)
    db.commit()
    # Another tenant's alias/global email is permitted as metadata, not account adoption.
    new_alias = foreign.user_name
    operations = [{"op": "replace", "value": {"userName": new_alias.upper()}}]
    assert patch(client, foreign_token, mapping.id, operations).status_code == 404
    result = patch(client, token, mapping.id, operations)
    assert result.status_code == 200 and result.json()["id"] == str(mapping.id)
    assert result.json()["userName"] == new_alias
    for alias, expected in ((old_alias, 0), (new_alias.upper(), 1)):
        filtered = client.get(
            "/scim/v2/Users", headers=auth(token), params={"filter": f'userName eq "{alias}"'}
        )
        assert filtered.json()["totalResults"] == expected
    db.refresh(user)
    db.refresh(identity)
    db.refresh(mapping)
    assert mapping.user_id == user.id
    assert (
        user.email,
        user.password_hash,
        user.auth_version,
        user.force_password_change,
    ) == canonical
    assert identity.subject == "stable-qa-subject" and identity.user_id == user.id
    assert (
        post(client, token, old_alias).status_code == 409
    )  # no adoption of original global account
    assert post(client, token, new_alias).status_code == 409  # no duplicate alias


def test_alias_conflict_is_atomic_case_insensitive_and_reserves_tombstones(scim_client):
    client, db, token, _, mapping, *_ = scim_client
    other = post(client, token, f"alias-{uuid.uuid4().hex}@example.com", "other-id").json()
    assert client.delete(f"/scim/v2/Users/{other['id']}", headers=auth(token)).status_code == 204
    response = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"externalId": "rollback", "active": False}},
            {"op": "replace", "path": "userName", "value": other["userName"].upper()},
        ],
    )
    assert response.status_code == 409 and response.json()["scimType"] == "uniqueness"
    db.refresh(mapping)
    assert mapping.external_id == "external-a"
    member = db.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == mapping.user_id)
    )
    assert member.status.value == "active"


def test_public_locations_ignore_host_and_forwarded_header_input(scim_client, monkeypatch):
    client, _, token, _, mapping, *_ = scim_client
    monkeypatch.setattr(settings, "scim_public_base_url", "https://public-scim.example.test")
    headers = {
        **auth(token),
        "Forwarded": "host=attacker.example;proto=http",
        "X-Forwarded-Host": "attacker.example",
        "X-Forwarded-Proto": "http",
    }
    expected = f"https://public-scim.example.test/scim/v2/Users/{mapping.id}"
    assert (
        client.get(f"/scim/v2/Users/{mapping.id}", headers=headers).json()["meta"]["location"]
        == expected
    )
    assert (
        client.get("/scim/v2/Users", headers=headers).json()["Resources"][0]["meta"]["location"]
        == expected
    )
    response = client.post(
        "/scim/v2/Users",
        headers=headers,
        json={
            "userName": f"public-{uuid.uuid4().hex}@example.com",
            "emails": [{"type": "work", "value": "contact@example.com"}],
        },
    )
    assert response.status_code == 201
    assert response.headers["location"] == response.json()["meta"]["location"]
    assert response.headers["location"].startswith(
        "https://public-scim.example.test/scim/v2/Users/"
    )
    assert "localhost" not in response.headers["location"]
    renamed = patch(
        client,
        token,
        mapping.id,
        [{"op": "replace", "value": {"userName": "new-alias@example.com"}}],
    )
    assert renamed.json()["meta"]["location"] == expected


def test_missing_public_origin_fails_before_provisioning(scim_client, monkeypatch):
    client, db, token, *_ = scim_client
    monkeypatch.setattr(settings, "scim_public_base_url", None)
    email = f"unconfigured-{uuid.uuid4().hex}@example.com"
    assert post(client, token, email).status_code == 503
    assert db.scalar(select(User).where(User.email == email)) is None


@pytest.mark.parametrize(
    "origin",
    [
        "http://public.example.com",
        "https://localhost:8001",
        "https://127.0.0.1",
        "https://[::1]",
        "https://user:password@public.example.com",
        "https://public.example.com/path",
        "https://public.example.com?host=attacker",
        "https://public.example.com#fragment",
        "https://public.example.com:invalid",
        "https://public.example.com\n",
    ],
)
def test_unsafe_public_origin_configuration_is_rejected(origin):
    with pytest.raises(ValidationError):
        _SettingsWithoutEnv(scim_public_base_url=origin)


def test_public_origin_trailing_slash_is_normalized():
    assert (
        _SettingsWithoutEnv(scim_public_base_url="https://public.example.com/").scim_public_base_url
        == "https://public.example.com"
    )
