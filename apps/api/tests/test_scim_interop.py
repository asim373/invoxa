"""Regressions for captured Microsoft Validator requests; no external secrets."""

# ruff: noqa: F811
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apps.api.app import scim
from apps.api.app.models import (
    OrganizationMember,
    OrganizationMemberRole,
    ScimProvisioningEvent,
)
from apps.api.tests.test_scim_lifecycle import patch_active, post
from apps.api.tests.test_scim_read import auth, scim_client  # noqa: F401


def patch(client, token, identifier, operations):
    return client.patch(
        f"/scim/v2/Users/{identifier}",
        headers=auth(token),
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": operations,
        },
    )


def test_duplicate_post_conflicts_without_reactivating_or_auditing(scim_client):
    client, db, token, *_ = scim_client
    email = f"duplicate-{uuid.uuid4().hex}@example.com"
    created = post(client, token, email, "duplicate-external")
    identifier = created.json()["id"]
    assert created.status_code == 201
    assert patch_active(client, token, identifier, False).status_code == 200
    events_before = len(db.scalars(select(ScimProvisioningEvent)).all())
    repeated = post(client, token, email.upper(), "duplicate-external")
    assert repeated.status_code == 409 and repeated.json()["scimType"] == "uniqueness"
    current = client.get(f"/scim/v2/Users/{identifier}", headers=auth(token)).json()
    assert current["active"] is False and current["id"] == identifier
    assert len(db.scalars(select(ScimProvisioningEvent)).all()) == events_before


def test_validator_replace_forms_preserve_global_identity_and_tenant(scim_client):
    client, db, token, foreign_token, mapping, _, user, _ = scim_client
    original = (user.email, user.password_hash, user.auth_version, user.is_active)
    operations = [
        {"op": "replace", "path": 'emails[type eq "work"].value', "value": "NEW-WORK@example.com"},
        {"op": "replace", "value": {"externalId": "updated-external", "active": False}},
    ]
    assert patch(client, foreign_token, mapping.id, operations).status_code == 404
    result = patch(client, token, mapping.id, operations)
    assert result.status_code == 200
    body = result.json()
    assert body["externalId"] == "updated-external" and body["active"] is False
    assert body["emails"][0]["value"] == "new-work@example.com"
    assert body["userName"] == original[0]
    db.refresh(user)
    assert (user.email, user.password_hash, user.auth_version, user.is_active) == original
    filtered = client.get(
        "/scim/v2/Users",
        headers=auth(token),
        params={
            "filter": 'emails[type eq "work"].value eq "new-work@example.com"',
        },
    )
    assert filtered.json()["Resources"][0]["id"] == str(mapping.id)


def test_invalid_pathless_alias_is_atomic(scim_client):
    client, db, token, _, mapping, _, user, _ = scim_client
    original_email = user.email
    result = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"externalId": "must-not-commit", "active": False}},
            {"op": "replace", "value": {"userName": "invalid-alias"}},
        ],
    )
    assert result.status_code == 400 and result.json()["scimType"] == "invalidValue"
    current = client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()
    assert current["active"] is True and current["externalId"] == "external-a"
    assert current["userName"] == original_email
    assert not db.scalars(select(ScimProvisioningEvent)).all()
    unchanged = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"userName": original_email.upper()}},
        ],
    )
    assert unchanged.status_code == 200


def test_preview_external_id_sequence_is_bounded_and_last_write_wins(scim_client):
    client, _, token, _, mapping, *_ = scim_client
    response = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "remove", "path": "externalId"},
            {"op": "add", "value": {"externalId": "intermediate"}},
            {"op": "replace", "value": {"externalId": "final"}},
        ],
    )
    assert response.status_code == 200 and response.json()["externalId"] == "final"
    removed = patch(client, token, mapping.id, [{"op": "remove", "path": "externalId"}])
    assert removed.status_code == 200 and removed.json()["externalId"] is None


def test_external_id_conflict_rolls_back_all_profile_and_lifecycle_changes(scim_client):
    client, db, token, _, mapping, *_ = scim_client
    created = post(client, token, f"conflict-{uuid.uuid4().hex}@example.com", "reserved")
    assert created.status_code == 201
    before = len(db.scalars(select(ScimProvisioningEvent)).all())
    result = patch(
        client,
        token,
        mapping.id,
        [
            {
                "op": "replace",
                "path": 'emails[type eq "work"].value',
                "value": "changed@example.com",
            },
            {"op": "replace", "value": {"externalId": "reserved", "active": False}},
        ],
    )
    assert result.status_code == 409 and result.json()["scimType"] == "uniqueness"
    current = client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()
    assert current["externalId"] == "external-a" and current["active"] is True
    assert current["emails"][0]["value"] != "changed@example.com"
    assert len(db.scalars(select(ScimProvisioningEvent)).all()) == before


@pytest.mark.parametrize("value", [None, "", "bad-email", "x" * 321])
def test_invalid_work_email_rolls_back_external_id(scim_client, value):
    client, _, token, _, mapping, *_ = scim_client
    result = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"externalId": "must-not-commit"}},
            {"op": "replace", "path": 'emails[type eq "work"].value', "value": value},
        ],
    )
    assert result.status_code == 400 and result.json()["scimType"] == "invalidValue"
    assert (
        client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()["externalId"]
        == "external-a"
    )


def test_database_uniqueness_failure_is_controlled_and_atomic(scim_client, monkeypatch):
    client, db, token, _, mapping, *_ = scim_client

    def fail(*args, **kwargs):
        raise IntegrityError("synthetic", {}, Exception("synthetic"))

    monkeypatch.setattr(scim, "_event", fail)
    response = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"externalId": "rollback", "active": False}},
        ],
    )
    assert response.status_code == 409 and response.json()["scimType"] == "uniqueness"
    db.refresh(mapping)
    assert mapping.external_id == "external-a"
    member = db.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == mapping.user_id)
    )
    db.refresh(member)
    assert member.status.value == "active"


@pytest.mark.parametrize("role", [OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN])
def test_profile_patch_protects_current_privileged_role(scim_client, role):
    client, db, token, _, mapping, *_ = scim_client
    member = db.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == mapping.user_id)
    )
    member.role = role
    db.commit()
    response = patch(
        client,
        token,
        mapping.id,
        [
            {"op": "replace", "value": {"externalId": "must-not-change", "active": False}},
        ],
    )
    assert response.status_code == 403
    db.refresh(member)
    assert member.status.value == "active" and member.role == role


def test_profile_changes_do_not_commit_when_reactivation_exceeds_quota(scim_client):
    client, _, token, _, mapping, *_ = scim_client
    assert patch_active(client, token, mapping.id, False).status_code == 200
    for index in range(6):
        assert (
            post(client, token, f"fill-{index}-{uuid.uuid4().hex}@example.com").status_code == 201
        )
    response = patch(
        client,
        token,
        mapping.id,
        [
            {
                "op": "replace",
                "path": 'emails[type eq "work"].value',
                "value": "rollback@example.com",
            },
            {"op": "replace", "value": {"externalId": "must-not-change", "active": True}},
        ],
    )
    assert response.status_code == 409
    current = client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()
    assert current["externalId"] == "external-a" and current["active"] is False
    assert current["emails"][0]["value"] != "rollback@example.com"
