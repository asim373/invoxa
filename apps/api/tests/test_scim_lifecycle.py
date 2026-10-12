"""Part 3 SCIM lifecycle checks against the real FastAPI routes."""

# ruff: noqa: F811  # pytest fixture imported for discovery and injected by name.

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from apps.api.app import scim
from apps.api.app.models import (
    Document,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    ScimProvisioningEvent,
    ScimUserMapping,
    User,
)
from apps.api.tests.test_scim_read import auth, scim_client  # noqa: F401


def user_payload(email: str, external_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "schemas": [scim.SCIM_USER_SCHEMA],
        "userName": email,
        "emails": [{"type": "work", "value": email}],
    }
    if external_id is not None:
        payload["externalId"] = external_id
    return payload


def post(client, token: str, email: str, external_id: str | None = None):
    return client.post("/scim/v2/Users", json=user_payload(email, external_id), headers=auth(token))


def patch_active(client, token: str, identifier: uuid.UUID | str, active: bool):
    return client.patch(
        f"/scim/v2/Users/{identifier}",
        json={
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "replace", "path": "active", "value": active}],
        },
        headers=auth(token),
    )


def count(session, model, *predicates) -> int:
    return session.scalar(select(func.count()).select_from(model).where(*predicates)) or 0


def test_distinct_work_email_round_trips_without_becoming_login_identity(scim_client):
    client, session, token, foreign_token, _, _, _, _ = scim_client
    login = f"login-{uuid.uuid4().hex}@example.com"
    work = f"contact-{uuid.uuid4().hex}@example.com"
    unrelated = User(email=work, password_hash="unrelated-password")
    session.add(unrelated)
    session.commit()
    payload = user_payload(login.upper(), "distinct-work")
    payload["emails"] = [{"type": "work", "value": work.upper()}]
    response = client.post("/scim/v2/Users", json=payload, headers=auth(token))
    assert response.status_code == 201
    identifier = response.json()["id"]
    assert response.json()["userName"] == login
    assert response.json()["emails"][0]["value"] == work
    mapping = session.get(ScimUserMapping, uuid.UUID(identifier))
    assert mapping.work_email == work
    assert mapping.user_id != unrelated.id
    assert session.get(User, mapping.user_id).email == login
    assert count(session, OrganizationMember, OrganizationMember.user_id == unrelated.id) == 0
    for expression in (
        f'userName eq "{login.upper()}"',
        f'emails[type eq "work"].value eq "{work.upper()}"',
    ):
        result = client.get("/scim/v2/Users", params={"filter": expression}, headers=auth(token))
        assert result.json()["totalResults"] == 1
        assert result.json()["Resources"][0]["id"] == identifier
        foreign = client.get(
            "/scim/v2/Users", params={"filter": expression}, headers=auth(foreign_token)
        )
        assert foreign.json()["totalResults"] == 0
    assert (
        client.get(
            "/scim/v2/Users",
            params={"filter": f'emails[type eq "work"].value eq "{login}"'},
            headers=auth(token),
        ).json()["totalResults"]
        == 0
    )
    assert patch_active(client, token, identifier, False).status_code == 200
    assert patch_active(client, token, identifier, True).status_code == 200
    fetched = client.get(f"/scim/v2/Users/{identifier}", headers=auth(token))
    assert fetched.json()["emails"][0]["value"] == work
    assert fetched.json()["userName"] == login
    conflicting = dict(payload)
    conflicting["emails"] = [{"type": "work", "value": "replacement@example.com"}]
    assert client.post("/scim/v2/Users", json=conflicting, headers=auth(token)).status_code == 409
    assert (
        client.get(f"/scim/v2/Users/{identifier}", headers=auth(token)).json()["emails"][0]["value"]
        == work
    )


def test_new_user_provisions_member_mapping_and_secret_free_audit(scim_client, caplog):
    client, session, token, _, _, _, _, _ = scim_client
    email = f"new-{uuid.uuid4().hex}@example.com"
    response = post(client, token, email.upper(), "id-one")
    assert response.status_code == 201
    body = response.json()
    assert response.headers["location"] == f"https://testserver/scim/v2/Users/{body['id']}"
    assert response.headers["content-type"].startswith("application/scim+json")
    assert body["userName"] == email and body["externalId"] == "id-one"
    assert (
        body["active"] is True
        and body["id"] == client.get(response.headers["location"], headers=auth(token)).json()["id"]
    )
    user = session.scalar(select(User).where(User.email == email))
    assert user is not None and user.password_hash and user.password_hash != email
    membership = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == user.id)
    )
    assert membership is not None and membership.role is OrganizationMemberRole.MEMBER
    assert membership.status is OrganizationMemberStatus.ACTIVE
    assert count(session, ScimUserMapping, ScimUserMapping.user_id == user.id) == 1
    events = session.scalars(select(ScimProvisioningEvent)).all()
    assert any(
        event.action == "provision" and event.resource_id == uuid.UUID(body["id"])
        for event in events
    )
    serialized = str(body) + str([event.__dict__ for event in events]) + caplog.text
    assert token not in serialized and user.password_hash not in serialized
    assert "Authorization" not in serialized and "password" not in str(body).casefold()


@pytest.mark.parametrize(
    "change",
    [
        {"userName": "not-an-email"},
        {"emails": [{"type": "work", "value": "not-an-email"}]},
        {
            "emails": [
                {"type": "work", "value": "x@example.com"},
                {"type": "work", "value": "y@example.com"},
            ]
        },
        {"password": "attacker-password"},
        {"roles": ["owner"]},
        {"role": "admin"},
        {"active": False},
    ],
)
def test_invalid_or_privileged_identity_payload_rejected(scim_client, change):
    client, session, token, _, _, _, _, _ = scim_client
    email = f"invalid-{uuid.uuid4().hex}@example.com"
    payload = user_payload(email)
    payload.update(change)
    response = client.post("/scim/v2/Users", json=payload, headers=auth(token))
    assert response.status_code == 400
    assert count(session, User, User.email == email) == 0


def test_alias_never_adopts_global_account_even_with_same_workspace_membership(scim_client):
    client, session, token, _, _, _, _, other_org = scim_client
    unrelated = User(email=f"unrelated-{uuid.uuid4().hex}@example.com", password_hash="secret")
    session.add(unrelated)
    session.commit()
    response = post(client, token, unrelated.email)
    assert response.status_code == 409 and "existing" in response.json()["detail"]
    assert count(session, OrganizationMember, OrganizationMember.user_id == unrelated.id) == 0
    org = session.scalar(select(Organization).where(Organization.id != other_org.id))
    assert org is not None
    member = OrganizationMember(
        organization_id=org.id,
        user_id=unrelated.id,
        role=OrganizationMemberRole.MEMBER,
        status=OrganizationMemberStatus.INACTIVE,
    )
    session.add(member)
    session.commit()
    associated = post(client, token, unrelated.email, "linked")
    assert associated.status_code == 409 and associated.json()["scimType"] == "uniqueness"
    repeated = post(client, token, unrelated.email, "linked")
    assert repeated.status_code == 409
    assert repeated.json()["scimType"] == "uniqueness"
    assert count(session, User, User.email == unrelated.email) == 1
    assert count(session, OrganizationMember, OrganizationMember.user_id == unrelated.id) == 1
    assert count(session, ScimUserMapping, ScimUserMapping.user_id == unrelated.id) == 0


def test_duplicate_external_id_and_last_seat_rollback(scim_client):
    client, session, token, _, _, _, _, other_org = scim_client
    org = session.scalar(select(Organization).where(Organization.id != other_org.id))
    assert org is not None
    first_email = f"first-{uuid.uuid4().hex}@example.com"
    assert post(client, token, first_email, "same-external").status_code == 201
    duplicate_email = f"duplicate-{uuid.uuid4().hex}@example.com"
    duplicate = post(client, token, duplicate_email, "same-external")
    assert duplicate.status_code == 409 and duplicate.json()["scimType"] == "uniqueness"
    assert count(session, User, User.email == duplicate_email) == 0
    # Existing member plus one newly provisioned account: fill to 5/6.
    for _ in range(3):
        email = f"fill-{uuid.uuid4().hex}@example.com"
        assert post(client, token, email).status_code == 201
    assert (
        count(
            session,
            OrganizationMember,
            OrganizationMember.organization_id == org.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
        == 5
    )
    final_email = f"final-{uuid.uuid4().hex}@example.com"
    assert post(client, token, final_email).status_code == 201
    rejected_email = f"over-{uuid.uuid4().hex}@example.com"
    rejected = post(client, token, rejected_email)
    assert rejected.status_code == 409
    assert count(session, User, User.email == rejected_email) == 0
    assert (
        count(
            session,
            OrganizationMember,
            OrganizationMember.organization_id == org.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
        == 6
    )
    assert (
        count(
            session,
            ScimProvisioningEvent,
            ScimProvisioningEvent.organization_id == org.id,
            ScimProvisioningEvent.action == "provision",
        )
        == 5
    )


def test_patch_deactivate_reactivate_is_scoped_and_multiop_atomic(scim_client):
    client, session, token, _, mapping, _, user, _ = scim_client
    original_version = user.auth_version
    assert patch_active(client, token, mapping.id, False).json()["active"] is False
    assert patch_active(client, token, mapping.id, False).status_code == 200
    invalid = client.patch(
        f"/scim/v2/Users/{mapping.id}",
        json={
            "Operations": [
                {"op": "replace", "path": "active", "value": True},
                {"op": "replace", "path": "userName", "value": "invalid-alias"},
            ]
        },
        headers=auth(token),
    )
    assert invalid.status_code == 400
    assert client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()["active"] is False
    assert patch_active(client, token, mapping.id, True).json()["active"] is True
    assert patch_active(client, token, mapping.id, True).status_code == 200
    session.refresh(user)
    assert user.is_active is True and user.auth_version == original_version
    assert user.email != "takeover@example.com"


@pytest.mark.parametrize("role", [OrganizationMemberRole.OWNER, OrganizationMemberRole.ADMIN])
def test_promoted_privileged_mapping_cannot_be_patched_or_deleted(scim_client, role):
    client, session, token, _, mapping, _, _, _ = scim_client
    member = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == mapping.user_id)
    )
    assert member is not None
    member.role = role
    session.commit()
    assert patch_active(client, token, mapping.id, False).status_code == 403
    assert client.delete(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 403
    session.refresh(member)
    assert member.status is OrganizationMemberStatus.ACTIVE


def test_delete_tombstones_only_workspace_membership_and_preserves_document(scim_client):
    client, session, token, _, mapping, _, user, other_org = scim_client
    original_version = user.auth_version
    org = session.scalar(select(Organization).where(Organization.id != other_org.id))
    assert org is not None
    document = Document(
        original_filename="record.pdf",
        mime_type="application/pdf",
        file_size=1,
        checksum="a" * 64,
        storage_location="qa/record.pdf",
        owner_id=user.id,
        organization_id=org.id,
    )
    session.add(document)
    session.commit()
    assert client.delete(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 204
    assert client.delete(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 404
    assert client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).status_code == 404
    assert client.get("/scim/v2/Users", headers=auth(token)).json()["totalResults"] == 0
    session.refresh(mapping)
    session.refresh(user)
    assert mapping.deactivated_at is not None
    assert session.get(User, user.id) is not None and session.get(Document, document.id) is not None
    assert user.is_active and user.auth_version == original_version
    assert (
        count(
            session,
            ScimProvisioningEvent,
            ScimProvisioningEvent.resource_id == mapping.id,
            ScimProvisioningEvent.action == "delete",
        )
        == 1
    )


def test_foreign_resource_mutation_and_header_override_denied(scim_client):
    client, _, token, _, _, foreign, _, other_org = scim_client
    header = {**auth(token), "X-Organization-ID": str(other_org.id)}
    assert patch_active(client, token, foreign.id, False).status_code == 404
    assert client.delete(f"/scim/v2/Users/{foreign.id}", headers=header).status_code == 404
    email = f"header-{uuid.uuid4().hex}@example.com"
    created = client.post("/scim/v2/Users", json=user_payload(email), headers=header)
    assert created.status_code == 201
    assert (
        client.get(f"/scim/v2/Users/{created.json()['id']}", headers=auth(token)).status_code == 200
    )


def test_audit_write_failure_rolls_back_patch(scim_client, monkeypatch):
    client, session, token, _, mapping, _, _, _ = scim_client

    def failed_event(*_args, **_kwargs):
        raise SQLAlchemyError("forced audit failure")

    monkeypatch.setattr(scim, "_event", failed_event)
    failed = patch_active(client, token, mapping.id, False)
    assert failed.status_code == 500
    member = session.scalar(
        select(OrganizationMember).where(OrganizationMember.user_id == mapping.user_id)
    )
    assert member is not None
    session.refresh(member)
    assert member.status is OrganizationMemberStatus.ACTIVE
    assert (
        count(session, ScimProvisioningEvent, ScimProvisioningEvent.resource_id == mapping.id) == 0
    )


def test_audit_write_failure_rolls_back_provisioning(scim_client, monkeypatch):
    client, session, token, _, _, _, _, _ = scim_client
    email = f"audit-fail-{uuid.uuid4().hex}@example.com"

    def failed_event(*_args, **_kwargs):
        raise SQLAlchemyError("forced audit failure")

    monkeypatch.setattr(scim, "_event", failed_event)
    response = post(client, token, email)
    assert response.status_code == 500
    assert count(session, User, User.email == email) == 0
    assert count(session, ScimUserMapping, ScimUserMapping.user_name == email) == 0
    assert count(session, ScimProvisioningEvent) == 0


def test_multi_operation_patch_succeeds_and_invalid_ops_reject_without_mutation(scim_client):
    client, session, token, _, mapping, _, user, _ = scim_client
    before_version = user.auth_version
    response = client.patch(
        f"/scim/v2/Users/{mapping.id}",
        json={
            "Operations": [
                {"op": "replace", "path": "active", "value": False},
                {"op": "replace", "path": "active", "value": True},
            ]
        },
        headers=auth(token),
    )
    assert response.status_code == 200 and response.json()["active"] is True
    for operation in [
        {"op": "add", "path": "active", "value": False},
        {"op": "replace", "path": "password", "value": "secret"},
        {"op": "replace", "path": "emails", "value": []},
        {"op": "replace", "path": "active", "value": "false"},
    ]:
        rejected = client.patch(
            f"/scim/v2/Users/{mapping.id}",
            json={"Operations": [{"op": "replace", "path": "active", "value": False}, operation]},
            headers=auth(token),
        )
        assert rejected.status_code == 400
        assert (
            client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()["active"] is True
        )
    session.refresh(user)
    assert user.auth_version == before_version and user.is_active


def test_inactive_global_user_cannot_be_reactivated(scim_client):
    client, session, token, _, mapping, _, user, _ = scim_client
    assert patch_active(client, token, mapping.id, False).status_code == 200
    user.is_active = False
    session.commit()
    assert patch_active(client, token, mapping.id, True).status_code == 409
    assert client.get(f"/scim/v2/Users/{mapping.id}", headers=auth(token)).json()["active"] is False
