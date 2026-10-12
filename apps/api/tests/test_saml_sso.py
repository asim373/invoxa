"""Exercise actual PySAML2/XMLSec signatures in the supported API test image.

Run this suite where xmlsec1 is installed; absence is an error, not a skipped
security check. No external IdP, live database, or live services are used.
"""

import base64
import hashlib
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from saml2 import class_name, saml, samlp
from saml2.sigver import pre_signature_part, security_context, signed_instance_factory
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app import saml_sso
from apps.api.app.auth import create_access_token, hash_reset_token
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    OAuthLoginCode,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationSamlConfiguration,
    OrganizationStatus,
    SamlAssertionReplay,
    SamlAuthTransaction,
    SamlIdentity,
    ScimConfiguration,
    ScimCredential,
    ScimUserMapping,
    User,
    UserSession,
)
from apps.api.app.settings import settings


@pytest.fixture
def certificate(tmp_path: Path):
    return make_certificate(tmp_path)


def make_certificate(tmp_path: Path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated-test-idp")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    cert_file, key_file = tmp_path / "idp.crt", tmp_path / "idp.key"
    cert_file.write_text(pem)
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return pem, cert_file, key_file


@pytest.fixture
def context(monkeypatch: pytest.MonkeyPatch, certificate):
    monkeypatch.setattr(settings, "saml_public_base_url", "https://testserver")
    monkeypatch.setattr(settings, "saml_sp_key_file", None)
    monkeypatch.setattr(settings, "saml_sp_certificate_file", None)
    monkeypatch.setattr(settings, "rate_limit_enabled", False)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as database:
        user = User(email="saml-existing@example.com", password_hash="unused", auth_version=3)
        organization = Organization(name="SAML test", slug="saml-test", plan_code="enterprise")
        membership = OrganizationMember(
            user=user,
            organization=organization,
            role=OrganizationMemberRole.OWNER,
            status=OrganizationMemberStatus.ACTIVE,
        )
        configuration = OrganizationSamlConfiguration(
            organization=organization,
            enabled=True,
            idp_entity_id="https://idp.example.com/entity",
            idp_sso_url="https://idp.example.com/sso",
            signing_certificates=[certificate[0]],
            attribute_mappings={},
        )
        database.add_all([user, organization, membership, configuration])
        database.commit()
        app.dependency_overrides[get_db] = lambda: database
        with TestClient(app, base_url="https://testserver") as client:
            yield database, user, organization, membership, configuration, client
        app.dependency_overrides.clear()
    engine.dispose()


def start(context, return_path="/"):
    database, _, organization, _, _, client = context
    response = client.get(
        f"/auth/saml/{organization.slug}/start",
        params={"return_path": return_path},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    relay = parse_qs(urlsplit(response.headers["location"]).query)["RelayState"][0]
    transaction = database.scalar(
        select(SamlAuthTransaction).where(SamlAuthTransaction.relay_state == relay)
    )
    assert transaction is not None
    assert (
        transaction.browser_hash
        == hashlib.sha256(client.cookies.get(saml_sso.COOKIE).encode()).hexdigest()
    )
    return transaction


def signed_response(context, certificate, transaction, change=None):
    """Generate both signatures with the same library/XMLSec used by production."""
    assert shutil.which("xmlsec1"), "Run signed SAML tests in the XMLSec-enabled API test image"
    _, user, organization, _, configuration, _ = context
    entity, acs = saml_sso._urls(organization)
    now = datetime.now(UTC)
    instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expiry = (now + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assertion = saml.Assertion(
        id=f"assertion-{uuid.uuid4().hex}",
        version="2.0",
        issue_instant=instant,
        issuer=saml.Issuer(text=configuration.idp_entity_id),
        subject=saml.Subject(
            name_id=saml.NameID(text="stable-test-subject", format=saml.NAMEID_FORMAT_PERSISTENT),
            subject_confirmation=[
                saml.SubjectConfirmation(
                    method=saml.SCM_BEARER,
                    subject_confirmation_data=saml.SubjectConfirmationData(
                        recipient=acs,
                        in_response_to=transaction.request_id,
                        not_on_or_after=expiry,
                    ),
                )
            ],
        ),
        conditions=saml.Conditions(
            not_before=(now - timedelta(seconds=10)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            not_on_or_after=expiry,
            audience_restriction=[saml.AudienceRestriction(audience=[saml.Audience(text=entity)])],
        ),
        authn_statement=[
            saml.AuthnStatement(
                authn_instant=instant,
                session_index="test-session",
                authn_context=saml.AuthnContext(
                    authn_context_class_ref=saml.AuthnContextClassRef(
                        text=saml.AUTHN_PASSWORD_PROTECTED,
                    )
                ),
            )
        ],
        attribute_statement=[
            saml.AttributeStatement(
                attribute=[
                    saml.Attribute(
                        name="urn:oid:0.9.2342.19200300.100.1.3",
                        name_format=saml.NAME_FORMAT_URI,
                        attribute_value=[saml.AttributeValue(text=user.email)],
                    )
                ]
            )
        ],
    )
    response = samlp.Response(
        id=f"response-{uuid.uuid4().hex}",
        version="2.0",
        issue_instant=instant,
        destination=acs,
        in_response_to=transaction.request_id,
        issuer=saml.Issuer(text=configuration.idp_entity_id),
        status=samlp.Status(status_code=samlp.StatusCode(value=samlp.STATUS_SUCCESS)),
        assertion=[assertion],
    )
    if change:
        change(response, assertion)
    config = saml_sso._sp_config(organization, configuration)
    config.setattr("", "key_file", str(certificate[2]))
    config.setattr("", "cert_file", str(certificate[1]))
    security = security_context(config)
    assert security is not None
    for item in (assertion, response):
        item.signature = pre_signature_part(
            item.id,
            security.my_cert,
            sign_alg=saml_sso.SIGNATURE_ALGORITHM,
            digest_alg=saml_sso.DIGEST_ALGORITHM,
        )
    xml = signed_instance_factory(
        response,
        security,
        [
            (class_name(assertion), assertion.id),
            (class_name(response), response.id),
        ],
    )
    assert isinstance(xml, str | bytes)
    return base64.b64encode(xml.encode() if isinstance(xml, str) else xml).decode()


def post(context, transaction, encoded, organization_id=None, follow_redirects=True):
    return context[-1].post(
        f"/auth/saml/{organization_id or context[2].id}/acs",
        data={"RelayState": transaction.relay_state, "SAMLResponse": encoded},
        follow_redirects=follow_redirects,
    )


def test_scim_alias_rename_does_not_change_signed_saml_login_identity(
    context, certificate, monkeypatch
):
    from apps.api.app.scim import _hash

    database, user, organization, membership, _, client = context
    monkeypatch.setattr(settings, "scim_public_base_url", "https://testserver")
    membership.role = OrganizationMemberRole.MEMBER
    mapping = ScimUserMapping(
        organization_id=organization.id, user_id=user.id, user_name=user.email
    )
    token = "invx_scim_" + "a" * 43
    database.add_all(
        [
            mapping,
            ScimConfiguration(organization_id=organization.id, enabled=True),
            ScimCredential(
                organization_id=organization.id, label="alias-saml", secret_hash=_hash(token)
            ),
        ]
    )
    database.commit()
    original_email = user.email
    renamed = client.patch(
        f"/scim/v2/Users/{mapping.id}",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "Operations": [{"op": "replace", "value": {"userName": "new-scim-alias@example.com"}}]
        },
    )
    assert renamed.status_code == 200
    assert renamed.json()["userName"] == "new-scim-alias@example.com"
    database.refresh(user)
    assert user.email == original_email and user.auth_version == 3
    transaction = start(context)
    response = post(context, transaction, signed_response(context, certificate, transaction))
    assert response.status_code == 200
    decoded = jwt.decode(
        response.json()["access_token"],
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    assert decoded["sub"] == str(user.id) and decoded["ver"] == 3
    assert database.scalar(select(func.count()).select_from(User)) == 1


def test_valid_signed_login_issues_existing_jwt_and_consumes_once(context, certificate):
    database, user, organization, _, _, client = context
    transaction = start(context)
    encoded = signed_response(context, certificate, transaction)
    response = post(context, transaction, encoded)
    assert response.status_code == 200, response.text
    token = jwt.decode(
        response.json()["access_token"],
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    assert token["sub"] == str(user.id)
    assert token["ver"] == 3
    session_id = uuid.UUID(token["sid"])
    assert token["sso_org"] == str(organization.id)
    session = database.get(UserSession, session_id)
    assert session is not None
    assert session.user_id == user.id
    assert session.auth_version == user.auth_version
    assert session.sso_organization_id == organization.id
    assert session.revoked_at is None
    session_expiry = session.expires_at
    if session_expiry.tzinfo is None:
        session_expiry = session_expiry.replace(tzinfo=UTC)
    assert session_expiry > datetime.now(UTC)
    assert (
        client.get(
            "/auth/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
        ).status_code
        == 200
    )
    database.refresh(transaction)
    assert transaction.consumed_at is not None
    assert database.scalar(select(func.count()).select_from(SamlAssertionReplay)) == 1
    assert database.scalar(select(func.count()).select_from(SamlIdentity)) == 1
    assert post(context, transaction, encoded).status_code == 400
    user.auth_version += 1
    database.commit()
    assert (
        client.get(
            "/auth/me", headers={"Authorization": f"Bearer {response.json()['access_token']}"}
        ).status_code
        == 401
    )


def test_valid_signed_frontend_login_uses_single_use_org_bound_session_handoff(
    context, certificate, monkeypatch
):
    database, user, organization, _, _, client = context
    monkeypatch.setattr(settings, "frontend_base_url", "https://app.example.test")
    transaction = start(context, return_path=saml_sso.FRONTEND_RETURN_PATH)
    encoded = signed_response(context, certificate, transaction)

    acs = post(context, transaction, encoded, follow_redirects=False)
    assert acs.status_code == 303
    location = urlsplit(acs.headers["location"])
    assert location.scheme == "https" and location.netloc == "app.example.test"
    assert location.query == ""
    assert "access_token" not in location.fragment
    code = parse_qs(location.fragment)["saml_code"][0]

    grant = database.scalar(select(OAuthLoginCode).where(OAuthLoginCode.user_id == user.id))
    assert grant is not None
    assert grant.sso_organization_id == organization.id
    assert grant.code_hash == hash_reset_token(code)
    assert database.scalar(select(func.count()).select_from(UserSession)) == 0

    exchanged = client.post(
        "/auth/saml/exchange",
        json={"code": code},
        headers={"X-Organization-ID": str(uuid.uuid4())},
    )
    assert exchanged.status_code == 200, exchanged.text
    claims = jwt.decode(
        exchanged.json()["access_token"],
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    session = database.get(UserSession, uuid.UUID(claims["sid"]))
    assert claims["sub"] == str(user.id)
    assert claims["sso_org"] == str(organization.id)
    assert session is not None
    assert session.user_id == user.id
    assert session.sso_organization_id == organization.id
    assert session.revoked_at is None
    database.refresh(grant)
    assert grant.used_at is not None
    assert client.post("/auth/saml/exchange", json={"code": code}).status_code == 400


def test_frontend_saml_handoff_uses_single_use_org_bound_code(context, monkeypatch):
    database, user, organization, _, _, client = context

    class FakeSamlClient:
        def __init__(self, config):
            self.config = config

        def prepare_for_authenticate(self, **kwargs):
            return "request-id", {
                "headers": [
                    (
                        "Location",
                        "https://idp.example.com/sso?RelayState=" + kwargs["relay_state"],
                    )
                ]
            }

    monkeypatch.setattr(saml_sso, "_sp_config", lambda *_: object())
    monkeypatch.setattr(saml_sso, "Saml2Client", FakeSamlClient)
    monkeypatch.setattr(
        saml_sso,
        "_validate_response",
        lambda *_: (
            "synthetic-assertion-id",
            "synthetic-stable-subject",
            user.email,
            datetime.now(UTC) + timedelta(minutes=4),
        ),
    )
    started = client.get(
        f"/auth/saml/{organization.slug}/start",
        params={"return_path": saml_sso.FRONTEND_RETURN_PATH},
        follow_redirects=False,
    )
    assert started.status_code == 302, started.text
    relay = parse_qs(urlsplit(started.headers["location"]).query)["RelayState"][0]
    transaction = database.scalar(
        select(SamlAuthTransaction).where(SamlAuthTransaction.relay_state == relay)
    )
    assert transaction is not None
    response = client.post(
        f"/auth/saml/{organization.id}/acs",
        data={"RelayState": relay, "SAMLResponse": "synthetic-only"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "saml_code=" in urlsplit(response.headers["location"]).fragment
    assert not urlsplit(response.headers["location"]).query
    code = parse_qs(urlsplit(response.headers["location"]).fragment)["saml_code"][0]
    code_row = database.scalar(select(OAuthLoginCode).where(OAuthLoginCode.user_id == user.id))
    assert code_row is not None
    assert code_row.code_hash == hash_reset_token(code)
    assert code_row.sso_organization_id == organization.id
    assert code not in code_row.code_hash
    assert database.scalar(select(func.count()).select_from(UserSession)) == 0
    assert transaction.consumed_at is not None
    assert client.post("/auth/google/exchange", json={"code": code}).status_code == 400
    database.refresh(code_row)
    assert code_row.used_at is None

    exchanged = client.post("/auth/saml/exchange", json={"code": code})
    assert exchanged.status_code == 200, exchanged.text
    session_token = exchanged.json()["access_token"]
    claims = jwt.decode(
        session_token,
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    assert claims["sub"] == str(user.id)
    assert claims["sso_org"] == str(organization.id)
    session = database.get(UserSession, uuid.UUID(claims["sid"]))
    assert session is not None and session.sso_organization_id == organization.id
    assert exchanged.json()["organization_id"] == str(organization.id)
    assert client.post("/auth/saml/exchange", json={"code": code}).status_code == 400


def test_frontend_saml_start_returns_safe_error_to_frontend(context):
    client = context[-1]
    response = client.get(
        "/auth/saml/no-such-workspace/start",
        params={"return_path": saml_sso.FRONTEND_RETURN_PATH},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "saml_error=unavailable" in response.headers["location"]


@pytest.mark.parametrize(
    "plan,role,expected",
    [
        ("enterprise", OrganizationMemberRole.OWNER, 200),
        ("standard", OrganizationMemberRole.OWNER, 403),
        ("professional", OrganizationMemberRole.OWNER, 403),
        ("enterprise", OrganizationMemberRole.ADMIN, 403),
        ("enterprise", OrganizationMemberRole.MEMBER, 403),
    ],
)
def test_configuration_requires_enterprise_owner(context, certificate, plan, role, expected):
    database, user, organization, membership, configuration, client = context
    organization.plan_code, membership.role = plan, role
    database.commit()
    headers = {
        "Authorization": f"Bearer {create_access_token(user.id, user.auth_version)}",
        "X-Organization-ID": str(organization.id),
    }
    response = client.put(
        "/organizations/saml",
        headers=headers,
        json={
            "enabled": False,
            "idp_entity_id": configuration.idp_entity_id,
            "idp_sso_url": configuration.idp_sso_url,
            "signing_certificates": [certificate[0]],
        },
    )
    assert response.status_code == expected, response.text
    assert client.get("/organizations/saml", headers=headers).status_code == expected
    assert client.delete("/organizations/saml", headers=headers).status_code == (
        204 if expected == 200 else expected
    )


@pytest.mark.parametrize(
    "defect",
    [
        "issuer",
        "assertion_issuer",
        "audience",
        "recipient",
        "destination",
        "request",
        "confirmation_request",
        "expired",
        "future",
        "unsigned_response",
        "unsigned_assertion",
        "invalid_signature",
        "malformed",
        "xxe",
        "sha1",
        "duplicate_assertion",
    ],
)
def test_saml_validation_rejects_invalid_assertions(context, certificate, defect):
    transaction = start(context)

    def change(response, assertion):
        if defect == "issuer":
            response.issuer.text = "https://foreign.example/idp"
        elif defect == "assertion_issuer":
            assertion.issuer.text = "https://foreign.example/idp"
        elif defect == "audience":
            assertion.conditions.audience_restriction[0].audience[0].text = "foreign"
        elif defect == "recipient":
            assertion.subject.subject_confirmation[
                0
            ].subject_confirmation_data.recipient = "foreign"
        elif defect == "destination":
            response.destination = "https://foreign.example/acs"
        elif defect == "request":
            response.in_response_to = "unknown-request"
        elif defect == "confirmation_request":
            assertion.subject.subject_confirmation[0].subject_confirmation_data.in_response_to = "x"
        elif defect == "expired":
            assertion.conditions.not_on_or_after = "2000-01-01T00:00:00Z"
        elif defect == "future":
            assertion.conditions.not_before = "2099-01-01T00:00:00Z"

    encoded = signed_response(context, certificate, transaction, change)
    raw = base64.b64decode(encoded).decode()
    if defect in {"unsigned_response", "unsigned_assertion", "sha1", "duplicate_assertion"}:
        response = samlp.response_from_string(raw)
        assert response is not None
        if defect == "unsigned_response":
            response.signature = None
        elif defect == "unsigned_assertion":
            response.assertion[0].signature = None
        elif defect == "sha1":
            response.signature.signed_info.signature_method.algorithm = (
                "http://www.w3.org/2000/09/xmldsig#rsa-sha1"
            )
        else:
            response.assertion.append(response.assertion[0])
        raw = str(response)
    elif defect == "invalid_signature":
        raw = raw.replace("stable-test-subject", "attacker-subject")
    elif defect == "malformed":
        raw = "<not-valid"
    elif defect == "xxe":
        raw = '<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><x>&e;</x>'
    result = post(context, transaction, base64.b64encode(raw.encode()).decode())
    assert result.status_code == 400
    assert result.json()["detail"] == saml_sso.FAILURE
    assert context[0].scalar(select(func.count()).select_from(SamlIdentity)) == 0
    assert context[0].scalar(select(func.count()).select_from(SamlAssertionReplay)) == 0


@pytest.mark.parametrize("defect", ["missing", "expired", "consumed", "browser", "configuration"])
def test_transaction_rejections(context, certificate, defect):
    transaction = start(context)
    encoded = signed_response(context, certificate, transaction)
    if defect == "expired":
        transaction.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif defect == "consumed":
        transaction.consumed_at = datetime.now(UTC)
    elif defect == "browser":
        context[-1].cookies.clear()
    elif defect == "configuration":
        context[4].attribute_mappings = {"email": "changed-email"}
    context[0].commit()
    if defect == "missing":
        result = context[-1].post(
            f"/auth/saml/{context[2].id}/acs",
            data={
                "RelayState": "does-not-exist",
                "SAMLResponse": encoded,
            },
        )
    else:
        result = post(context, transaction, encoded)
    assert result.status_code == 400
    if defect != "consumed":
        context[0].refresh(transaction)
        assert transaction.consumed_at is None


@pytest.mark.parametrize(
    "defect",
    [
        "inactive_user",
        "inactive_member",
        "missing_member",
        "inactive_org",
        "unknown_email",
        "changed_subject",
        "subject_collision",
        "force_password_change",
    ],
)
def test_existing_identity_and_membership_required_without_jit(context, certificate, defect):
    database, user, organization, membership, configuration, _ = context
    transaction = start(context)
    encoded = signed_response(context, certificate, transaction)
    if defect == "inactive_user":
        user.is_active = False
    elif defect == "inactive_member":
        membership.status = OrganizationMemberStatus.INACTIVE
    elif defect == "missing_member":
        database.delete(membership)
    elif defect == "inactive_org":
        organization.status = OrganizationStatus.INACTIVE
    elif defect == "unknown_email":
        user.email = "different-existing@example.com"
    elif defect == "force_password_change":
        user.force_password_change = True
    elif defect == "changed_subject":
        database.add(
            SamlIdentity(
                configuration_id=configuration.id, user_id=user.id, subject="original-other-subject"
            )
        )
    else:
        other = User(email="other@example.com", password_hash="unused")
        database.add(other)
        database.flush()
        database.add(
            SamlIdentity(
                configuration_id=configuration.id, user_id=other.id, subject="stable-test-subject"
            )
        )
    database.commit()
    before = (
        database.scalar(select(func.count()).select_from(User)),
        database.scalar(select(func.count()).select_from(OrganizationMember)),
    )
    assert post(context, transaction, encoded).status_code == 400
    assert before == (
        database.scalar(select(func.count()).select_from(User)),
        database.scalar(select(func.count()).select_from(OrganizationMember)),
    )
    database.refresh(transaction)
    assert transaction.consumed_at is None


def test_cross_tenant_response_and_transaction_rejected(context, certificate):
    transaction = start(context)
    encoded = signed_response(context, certificate, transaction)
    other = Organization(name="Other", slug="other", plan_code="enterprise")
    context[0].add(other)
    context[0].flush()
    context[0].add(
        OrganizationSamlConfiguration(
            organization=other,
            enabled=True,
            idp_entity_id=context[4].idp_entity_id,
            idp_sso_url=context[4].idp_sso_url,
            signing_certificates=[certificate[0]],
            attribute_mappings={},
        )
    )
    context[0].commit()
    assert post(context, transaction, encoded, other.id).status_code == 400
    assert context[0].scalar(select(func.count()).select_from(SamlIdentity)) == 0


def test_durable_assertion_replay_with_a_new_valid_transaction(context, certificate):
    first = start(context)
    encoded = signed_response(context, certificate, first)
    assert post(context, first, encoded).status_code == 200
    assertion_id = context[0].scalar(select(SamlAssertionReplay.assertion_id))
    second = start(context)
    replay = signed_response(
        context, certificate, second, lambda _, assertion: setattr(assertion, "id", assertion_id)
    )
    assert post(context, second, replay).status_code == 400
    assert context[0].scalar(select(func.count()).select_from(SamlAssertionReplay)) == 1
    context[0].refresh(second)
    assert second.consumed_at is None


@pytest.mark.parametrize("path", ["https://evil.example", "//evil.example", "/\\evil", "/admin"])
def test_open_redirect_rejected(context, path):
    response = context[-1].get(
        "/auth/saml/saml-test/start", params={"return_path": path}, follow_redirects=False
    )
    assert response.status_code == 400
    assert context[0].scalar(select(func.count()).select_from(SamlAuthTransaction)) == 0


def test_metadata_contains_public_material_only(context):
    result = context[-1].get("/auth/saml/saml-test/metadata")
    assert result.status_code == 200
    assert str(context[2].id) in result.text
    assert "AssertionConsumerService" in result.text
    assert "PRIVATE KEY" not in result.text


def test_signed_authn_request_uses_sha256_with_deployment_keys(context, certificate, monkeypatch):
    monkeypatch.setattr(settings, "saml_sp_key_file", certificate[2])
    monkeypatch.setattr(settings, "saml_sp_certificate_file", certificate[1])
    response = context[-1].get("/auth/saml/saml-test/start", follow_redirects=False)
    assert response.status_code == 302, response.text
    parameters = parse_qs(urlsplit(response.headers["location"]).query)
    assert parameters["SigAlg"] == [saml_sso.SIGNATURE_ALGORITHM]
    assert parameters["Signature"][0]


def test_certificate_rotation_trusts_both_pinned_certificates(context, certificate, tmp_path):
    directory = tmp_path / "rotated"
    directory.mkdir()
    rotated = make_certificate(directory)
    context[4].signing_certificates = [certificate[0], rotated[0]]
    context[0].commit()
    for signer in (certificate, rotated):
        transaction = start(context)
        assert (
            post(context, transaction, signed_response(context, signer, transaction)).status_code
            == 200
        )
    assert context[0].scalar(select(func.count()).select_from(SamlIdentity)) == 1


def test_untrusted_signer_is_rejected_even_with_embedded_certificate(context, tmp_path):
    directory = tmp_path / "untrusted"
    directory.mkdir()
    untrusted = make_certificate(directory)
    transaction = start(context)
    assert (
        post(context, transaction, signed_response(context, untrusted, transaction)).status_code
        == 400
    )
    assert context[0].scalar(select(func.count()).select_from(SamlAssertionReplay)) == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("idp_sso_url", "http://idp.example.com/sso"),
        ("idp_sso_url", "https://user:password@idp.example.com/sso"),
        ("idp_sso_url", "https://idp.example.com/sso#fragment"),
        ("idp_entity_id", " whitespace "),
        ("signing_certificates", ["not-a-certificate"]),
        ("attribute_mappings", {"organization_id": "tenant-attribute"}),
    ],
)
def test_unsafe_configuration_is_rejected(context, certificate, field, value):
    user, organization, config = context[1], context[2], context[4]
    payload = {
        "enabled": False,
        "idp_entity_id": config.idp_entity_id,
        "idp_sso_url": config.idp_sso_url,
        "signing_certificates": [certificate[0]],
    }
    payload[field] = value
    headers = {
        "Authorization": f"Bearer {create_access_token(user.id, user.auth_version)}",
        "X-Organization-ID": str(organization.id),
    }
    assert context[-1].put("/organizations/saml", headers=headers, json=payload).status_code == 422


def test_public_rate_limit_and_no_assertion_logging(context, certificate, monkeypatch, caplog):
    class RejectLimiter:
        def check(self, *_args, **_kwargs):
            from fastapi import HTTPException

            raise HTTPException(429, "Too many requests")

    app.dependency_overrides[saml_sso.get_rate_limiter] = RejectLimiter
    assert context[-1].get("/auth/saml/saml-test/start").status_code == 429
    assert context[-1].post(f"/auth/saml/{context[2].id}/acs").status_code == 429
    app.dependency_overrides.pop(saml_sso.get_rate_limiter)
    transaction = start(context)
    encoded = signed_response(context, certificate, transaction)
    raw = base64.b64decode(encoded).replace(b"stable-test-subject", b"private-test-subject")
    assert post(context, transaction, base64.b64encode(raw).decode()).status_code == 400
    assert "private-test-subject" not in caplog.text
    assert "<saml" not in caplog.text
