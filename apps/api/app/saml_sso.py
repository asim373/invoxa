"""Enterprise SP-initiated SAML; authenticate existing active memberships only."""

import base64
import hashlib
import json
import logging
import secrets
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from saml2 import BINDING_HTTP_POST, BINDING_HTTP_REDIRECT, md, saml, samlp
from saml2 import xmldsig as ds
from saml2.client import Saml2Client
from saml2.config import SPConfig
from saml2.metadata import entity_descriptor
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.auth import (
    AccessTokenResponse,
    get_current_user,
    hash_reset_token,
    issue_access_token,
    normalize_email,
)
from apps.api.app.database import get_db
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
    User,
)
from apps.api.app.plans import CAPABILITY_SAML_SSO, resolve_plan_entitlements
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.security_audit import add_security_event
from apps.api.app.settings import settings
from apps.api.app.tenant import TenantContext, get_current_tenant

router = APIRouter(prefix="/auth/saml", tags=["SAML authentication"])


def _management_limit(
    request: Request,
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> None:
    limiter.check("saml-management", client_identifier(request), 30, 300, fail_closed=True)


management_router = APIRouter(
    prefix="/organizations/saml",
    tags=["SAML configuration"],
    dependencies=[Depends(_management_limit)],
)
logger = logging.getLogger(__name__)
FAILURE = "SAML sign-in could not be completed. Contact your workspace administrator."
FEATURE_BLOCKED = (
    "This feature is not included in your current plan. Upgrade your plan to access it."
)
TRANSACTION_SECONDS = 300
CLOCK_SKEW = 120
MAX_ASSERTION_SECONDS = 600
MAX_RESPONSE_BYTES = 256 * 1024
COOKIE = "invoxa_saml_browser"
FRONTEND_RETURN_PATH = "/saml-login"


class SamlLoginCodeRequest(BaseModel):
    code: str = Field(min_length=32, max_length=256)


class SamlLoginResponse(BaseModel):
    access_token: str
    organization_id: str


SIGNATURE_ALGORITHM = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
DIGEST_ALGORITHM = "http://www.w3.org/2001/04/xmlenc#sha256"
ALLOWED_SIGNATURES = {
    SIGNATURE_ALGORITHM,
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha384",
    "http://www.w3.org/2001/04/xmldsig-more#rsa-sha512",
}
ALLOWED_DIGESTS = {
    DIGEST_ALGORITHM,
    "http://www.w3.org/2001/04/xmldsig-more#sha384",
    "http://www.w3.org/2001/04/xmlenc#sha512",
}

# PySAML2 can include assertion XML in its diagnostics. Block library logging,
# including lastResort handlers; application logs contain event names only.
for _name in ("saml2", "xmlschema", "elementpath"):
    _library_logger = logging.getLogger(_name)
    _library_logger.handlers = [logging.NullHandler()]
    _library_logger.propagate = False


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or "\\" in value
        or any(ord(char) < 33 for char in value)
    ):
        raise ValueError("A valid HTTPS URL without credentials or fragment is required.")
    return value


class SamlConfigurationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    idp_entity_id: str = Field(min_length=1, max_length=2048)
    idp_sso_url: str = Field(min_length=1, max_length=2048)
    signing_certificates: list[str] = Field(min_length=1, max_length=5)
    attribute_mappings: dict[str, str] = Field(default_factory=dict)

    @field_validator("idp_sso_url")
    @classmethod
    def validate_sso_url(cls, value: str) -> str:
        return _https_url(value)

    @field_validator("idp_entity_id")
    @classmethod
    def validate_entity_id(cls, value: str) -> str:
        if value != value.strip() or any(ord(char) < 33 for char in value):
            raise ValueError("IdP entity ID must be a nonblank stable identifier.")
        return value

    @field_validator("signing_certificates")
    @classmethod
    def validate_certificates(cls, values: list[str]) -> list[str]:
        fingerprints = set()
        normalized = []
        now = datetime.now(UTC)
        for value in values:
            if len(value) > 16_384:
                raise ValueError("Signing certificate is too large.")
            try:
                certificate = x509.load_pem_x509_certificate(value.encode())
            except ValueError:
                raise ValueError("A PEM X.509 signing certificate is required.") from None
            public_key = certificate.public_key()
            if not isinstance(public_key, rsa.RSAPublicKey) or public_key.key_size < 2048:
                raise ValueError("IdP signing certificates require RSA keys of at least 2048 bits.")
            if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
                raise ValueError("IdP signing certificate is not currently valid.")
            fingerprint = certificate.fingerprint(hashes.SHA256())
            if fingerprint in fingerprints:
                raise ValueError("Duplicate signing certificate.")
            fingerprints.add(fingerprint)
            normalized.append(certificate.public_bytes(serialization.Encoding.PEM).decode())
        return normalized

    @field_validator("attribute_mappings")
    @classmethod
    def validate_mappings(cls, values: dict[str, str]) -> dict[str, str]:
        if set(values) - {"email", "given_name", "family_name"}:
            raise ValueError("Unsupported SAML attribute mapping.")
        if any(not value.strip() or len(value) > 255 for value in values.values()):
            raise ValueError("Invalid SAML attribute mapping.")
        return values


class SamlConfigurationOutput(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    enabled: bool
    idp_entity_id: str
    idp_sso_url: str
    signing_certificates: list[str]
    attribute_mappings: dict[str, str]
    updated_at: datetime


def _entitled(organization: Organization) -> None:
    try:
        entitled = resolve_plan_entitlements(organization.plan_code).includes(CAPABILITY_SAML_SSO)
    except ValueError:
        entitled = False
    if not entitled:
        raise HTTPException(403, FEATURE_BLOCKED)


def _owner(tenant: TenantContext = Depends(get_current_tenant)) -> Organization:  # noqa: B008
    if tenant.organization is None or tenant.membership is None:
        raise HTTPException(403, "Select an active workspace.")
    _entitled(tenant.organization)
    if tenant.membership.role != OrganizationMemberRole.OWNER:
        raise HTTPException(403, "Only the workspace owner can manage SAML SSO.")
    return tenant.organization


def _configuration(database: Session, organization_id: uuid.UUID):
    return database.scalar(
        select(OrganizationSamlConfiguration).where(
            OrganizationSamlConfiguration.organization_id == organization_id
        )
    )


@management_router.get("", response_model=SamlConfigurationOutput)
def get_configuration(
    organization: Organization = Depends(_owner),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
):
    configuration = _configuration(database, organization.id)
    if configuration is None:
        raise HTTPException(404, "SAML SSO is not configured.")
    return configuration


@management_router.put("", response_model=SamlConfigurationOutput)
def put_configuration(
    payload: SamlConfigurationInput,
    organization: Organization = Depends(_owner),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
):
    database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    configuration = _configuration(database, organization.id)
    if configuration is None:
        configuration = OrganizationSamlConfiguration(organization_id=organization.id)
        database.add(configuration)
    elif configuration.idp_entity_id != payload.idp_entity_id:
        # An issuer change must not reuse links from a different identity authority.
        raise HTTPException(409, "Changing the configured IdP entity ID is not supported.")
    if organization.sso_enforced and not payload.enabled:
        raise HTTPException(409, "Disable mandatory SSO before disabling its SAML configuration.")
    for field, value in payload.model_dump().items():
        setattr(configuration, field, value)
    if payload.enabled:
        _sp_config(organization, configuration)
    database.flush()
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="saml_configuration_changed",
        resource_type="saml_configuration",
        resource_id=configuration.id,
        context={"enabled": payload.enabled},
    )
    database.commit()
    database.refresh(configuration)
    logger.info("saml_configuration_updated")
    return configuration


@management_router.delete("", status_code=204)
def disable_configuration(
    organization: Organization = Depends(_owner),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
):
    database.scalar(
        select(Organization).where(Organization.id == organization.id).with_for_update()
    )
    configuration = _configuration(database, organization.id)
    if configuration is None:
        raise HTTPException(404, "SAML SSO is not configured.")
    if organization.sso_enforced:
        raise HTTPException(409, "Disable mandatory SSO before disabling its SAML configuration.")
    configuration.enabled = False  # Preserve trusted identity history on disable.
    add_security_event(
        database,
        organization_id=organization.id,
        actor_user_id=current_user.id,
        action="saml_configuration_disabled",
        resource_type="saml_configuration",
        resource_id=configuration.id,
    )
    database.commit()
    logger.info("saml_configuration_disabled")
    return Response(status_code=204)


def _urls(organization: Organization) -> tuple[str, str]:
    if settings.saml_public_base_url is None:
        raise HTTPException(503, "SAML service configuration is unavailable.")
    try:
        base = _https_url(settings.saml_public_base_url).rstrip("/")
        if urlsplit(base).query or urlsplit(base).path:
            raise ValueError
    except ValueError:
        raise HTTPException(503, "SAML service configuration is unavailable.") from None
    return f"{base}/auth/saml/{organization.id}", f"{base}/auth/saml/{organization.id}/acs"


def _frontend_login_redirect(*, code: str | None = None, error: str | None = None):
    fragment = urlencode(
        {key: value for key, value in {"saml_code": code, "saml_error": error}.items() if value}
    )
    url = settings.frontend_base_url.rstrip("/") + "/"
    if fragment:
        # Fragments are not sent to the frontend server or included in referrers.
        url = f"{url}#{fragment}"
    response = RedirectResponse(url=url, status_code=303)
    response.delete_cookie(COOKIE, path="/auth/saml")
    return response


def _sp_config(
    organization: Organization, configuration: OrganizationSamlConfiguration
) -> SPConfig:
    entity_id, acs_url = _urls(organization)
    binary = shutil.which(settings.saml_xmlsec_binary)
    if binary is None:
        raise HTTPException(503, "SAML service configuration is unavailable.")
    # Library-generated trusted IdP metadata; no network metadata fetch.
    keys = [
        md.KeyDescriptor(
            use="signing",
            key_info=ds.KeyInfo(
                x509_data=[
                    ds.X509Data(
                        x509_certificate=[
                            ds.X509Certificate(text="".join(cert.strip().splitlines()[1:-1]))
                        ]
                    )
                ]
            ),
        )
        for cert in configuration.signing_certificates
    ]
    metadata = md.EntityDescriptor(
        entity_id=configuration.idp_entity_id,
        idpsso_descriptor=[
            md.IDPSSODescriptor(
                protocol_support_enumeration=samlp.NAMESPACE,
                key_descriptor=keys,
                single_sign_on_service=[
                    md.SingleSignOnService(
                        binding=BINDING_HTTP_REDIRECT, location=configuration.idp_sso_url
                    )
                ],
            )
        ],
    )
    configuration_values = {
        "entityid": entity_id,
        "xmlsec_binary": binary,
        "accepted_time_diff": CLOCK_SKEW,
        "only_use_keys_in_metadata": True,
        "allow_unknown_attributes": True,
        "metadata": {"inline": [str(metadata)]},
        "service": {
            "sp": {
                "endpoints": {"assertion_consumer_service": [(acs_url, BINDING_HTTP_POST)]},
                "allow_unsolicited": False,
                "want_response_signed": True,
                "want_assertions_signed": True,
                "authn_requests_signed": bool(settings.saml_sp_key_file),
                "signing_algorithm": SIGNATURE_ALGORITHM,
                "digest_algorithm": DIGEST_ALGORITHM,
                "name_id_policy_format": saml.NAMEID_FORMAT_PERSISTENT,
                "name_id_format_allow_create": False,
            }
        },
        "signing_algorithm": SIGNATURE_ALGORITHM,
        "digest_algorithm": DIGEST_ALGORITHM,
    }
    if settings.saml_sp_key_file and settings.saml_sp_certificate_file:
        configuration_values.update(
            key_file=str(settings.saml_sp_key_file),
            cert_file=str(settings.saml_sp_certificate_file),
        )
    try:
        return SPConfig().load(configuration_values)
    except Exception:
        raise HTTPException(503, "SAML service configuration is unavailable.") from None


def _enabled(database: Session, organization: Organization | None):
    if organization is None or organization.status != OrganizationStatus.ACTIVE:
        raise HTTPException(400, FAILURE)
    _entitled(organization)
    configuration = _configuration(database, organization.id)
    if configuration is None or not configuration.enabled:
        raise HTTPException(400, FAILURE)
    # Revalidate trusted certificates at authentication time, including expiry.
    try:
        SamlConfigurationInput.model_validate(configuration, from_attributes=True)
    except ValueError:
        raise HTTPException(400, FAILURE) from None
    return configuration


def _digest(configuration: OrganizationSamlConfiguration) -> str:
    value = json.dumps(
        {
            "enabled": configuration.enabled,
            "issuer": configuration.idp_entity_id,
            "sso": configuration.idp_sso_url,
            "certs": configuration.signing_certificates,
            "attributes": configuration.attribute_mappings,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode()).hexdigest()


@router.get("/{organization_slug}/metadata")
def metadata(organization_slug: str, database: Session = Depends(get_db)):  # noqa: B008
    organization = database.scalar(
        select(Organization).where(Organization.slug == organization_slug)
    )
    if organization is None:
        raise HTTPException(400, FAILURE)
    configuration = _enabled(database, organization)
    return Response(
        str(entity_descriptor(_sp_config(organization, configuration))),
        media_type="application/samlmetadata+xml",
    )


@router.get("/{organization_slug}/start")
def start(
    organization_slug: str,
    request: Request,
    return_path: str = "/",
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
):
    limiter.check(
        "saml-start",
        client_identifier(request),
        settings.saml_rate_limit,
        settings.saml_rate_window_seconds,
        fail_closed=True,
    )
    if return_path not in {"/", FRONTEND_RETURN_PATH}:
        raise HTTPException(400, "Invalid SAML return path.")
    try:
        organization = database.scalar(
            select(Organization).where(Organization.slug == organization_slug)
        )
        if organization is None:
            raise ValueError("Unavailable workspace")
        configuration = _enabled(database, organization)
        relay_state = secrets.token_urlsafe(32)
        browser_secret = secrets.token_urlsafe(32)
        request_id, result = Saml2Client(
            config=_sp_config(organization, configuration)
        ).prepare_for_authenticate(
            entityid=configuration.idp_entity_id,
            relay_state=relay_state,
            sigalg=SIGNATURE_ALGORITHM,
            digest_alg=DIGEST_ALGORITHM,
        )
        location = next(value for key, value in result["headers"] if key == "Location")
    except HTTPException:
        if return_path == FRONTEND_RETURN_PATH:
            return _frontend_login_redirect(error="unavailable")
        raise
    except Exception:
        if return_path == FRONTEND_RETURN_PATH:
            return _frontend_login_redirect(error="unavailable")
        raise HTTPException(400, FAILURE) from None
    database.add(
        SamlAuthTransaction(
            relay_state=relay_state,
            request_id=request_id,
            browser_hash=hashlib.sha256(browser_secret.encode()).hexdigest(),
            configuration_digest=_digest(configuration),
            organization_id=organization.id,
            configuration_id=configuration.id,
            return_path=return_path,
            expires_at=datetime.now(UTC) + timedelta(seconds=TRANSACTION_SECONDS),
        )
    )
    database.commit()
    response = RedirectResponse(location, status_code=302)
    response.set_cookie(
        COOKIE,
        browser_secret,
        secure=True,
        httponly=True,
        samesite="none",
        path="/auth/saml",
        max_age=TRANSACTION_SECONDS,
    )
    return response


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _saml_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _validate_response(
    encoded: str,
    organization: Organization,
    configuration: OrganizationSamlConfiguration,
    transaction: SamlAuthTransaction,
):
    if len(encoded) > MAX_RESPONSE_BYTES * 2:
        raise ValueError("Invalid response")
    raw = base64.b64decode(encoded, validate=True)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("Invalid response")
    # Use PySAML2's hardened parser for structure/policy inspection. Signature
    # verification is exclusively performed by PySAML2/XMLSec below.
    parsed = samlp.response_from_string(raw.decode("utf-8"))
    if parsed is None or len(parsed.assertion) != 1 or parsed.encrypted_assertion:
        raise ValueError("Invalid response")
    assertion = parsed.assertion[0]
    for item in (parsed, assertion):
        signature = item.signature
        if signature is None or signature.signed_info is None:
            raise ValueError("Unsigned response")
        signed = signature.signed_info
        if signed.signature_method.algorithm not in ALLOWED_SIGNATURES:
            raise ValueError("Invalid algorithm")
        if any(ref.digest_method.algorithm not in ALLOWED_DIGESTS for ref in signed.reference):
            raise ValueError("Invalid algorithm")
        if item.issuer is None or item.issuer.text != configuration.idp_entity_id:
            raise ValueError("Invalid issuer")
    entity_id, acs_url = _urls(organization)
    if parsed.destination != acs_url or parsed.in_response_to != transaction.request_id:
        raise ValueError("Invalid destination or transaction")
    if assertion.advice or assertion.subject is None:
        raise ValueError("Invalid subject")
    name_id = assertion.subject.name_id
    if (
        name_id is None
        or name_id.format != saml.NAMEID_FORMAT_PERSISTENT
        or not name_id.text
        or len(name_id.text) > 1024
        or name_id.name_qualifier not in (None, configuration.idp_entity_id)
        or name_id.sp_name_qualifier not in (None, entity_id)
    ):
        raise ValueError("Invalid stable subject")
    confirmations = assertion.subject.subject_confirmation
    if len(confirmations) != 1 or confirmations[0].method != saml.SCM_BEARER:
        raise ValueError("Invalid confirmation")
    confirmation = confirmations[0].subject_confirmation_data
    if (
        confirmation is None
        or confirmation.recipient != acs_url
        or confirmation.in_response_to != transaction.request_id
        or not confirmation.not_on_or_after
    ):
        raise ValueError("Invalid recipient")
    conditions = assertion.conditions
    if (
        conditions is None
        or not conditions.not_before
        or not conditions.not_on_or_after
        or not conditions.audience_restriction
    ):
        raise ValueError("Missing conditions")
    now = datetime.now(UTC)
    expiry = min(_saml_time(conditions.not_on_or_after), _saml_time(confirmation.not_on_or_after))
    issued = _saml_time(assertion.issue_instant)
    if (
        issued > now + timedelta(seconds=CLOCK_SKEW)
        or issued < now - timedelta(seconds=MAX_ASSERTION_SECONDS)
        or expiry > issued + timedelta(seconds=MAX_ASSERTION_SECONDS)
        or expiry <= now - timedelta(seconds=CLOCK_SKEW)
    ):
        raise ValueError("Invalid assertion lifetime")
    validated = Saml2Client(
        config=_sp_config(organization, configuration)
    ).parse_authn_request_response(
        encoded, BINDING_HTTP_POST, outstanding={transaction.request_id: transaction.return_path}
    )
    if validated is None or validated.assertion is None:
        raise ValueError("Invalid authentication")
    assertion = validated.assertion
    attribute_name = configuration.attribute_mappings.get(
        "email", "urn:oid:0.9.2342.19200300.100.1.3"
    )
    emails = [
        value.text
        for statement in assertion.attribute_statement
        for attribute in statement.attribute
        if attribute.name == attribute_name
        for value in attribute.attribute_value
    ]
    if len(emails) != 1 or not emails[0] or len(emails[0]) > 320:
        raise ValueError("Invalid email")
    if not assertion.id or len(assertion.id) > 255:
        raise ValueError("Invalid assertion ID")
    return assertion.id, name_id.text, normalize_email(emails[0]), expiry


@router.post("/{organization_id}/acs", response_model=AccessTokenResponse)
async def acs(
    organization_id: uuid.UUID,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
):
    limiter.check(
        "saml-acs",
        client_identifier(request),
        settings.saml_rate_limit,
        settings.saml_rate_window_seconds,
        fail_closed=True,
    )
    transaction: SamlAuthTransaction | None = None
    try:
        # Bounded body read before form parsing prevents oversized public requests.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_RESPONSE_BYTES * 2:
                raise ValueError("Invalid response")
        from urllib.parse import parse_qs

        form = parse_qs(body.decode("utf-8"), strict_parsing=True, max_num_fields=3)
        if set(form) != {"SAMLResponse", "RelayState"} or any(len(v) != 1 for v in form.values()):
            raise ValueError("Invalid form")
        relay_state, encoded = form["RelayState"][0], form["SAMLResponse"][0]
        organization = database.scalar(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
        if organization is None:
            raise ValueError("Unavailable workspace")
        transaction = database.scalar(
            select(SamlAuthTransaction)
            .where(
                SamlAuthTransaction.relay_state == relay_state,
                SamlAuthTransaction.organization_id == organization_id,
            )
            .with_for_update()
        )
        configuration = _enabled(database, organization)
        browser_secret = request.cookies.get(COOKIE, "")
        if (
            transaction is None
            or transaction.configuration_id != configuration.id
            or transaction.consumed_at is not None
            or _utc(transaction.expires_at) <= datetime.now(UTC)
            or transaction.return_path not in {"/", FRONTEND_RETURN_PATH}
            or not browser_secret
            or not secrets.compare_digest(
                transaction.browser_hash, hashlib.sha256(browser_secret.encode()).hexdigest()
            )
            or transaction.configuration_digest != _digest(configuration)
        ):
            raise ValueError("Invalid transaction")
        assertion_id, subject, email, expiry = _validate_response(
            encoded, organization, configuration, transaction
        )
        user = database.scalar(select(User).where(User.email == email).with_for_update())
        if user is None or not user.is_active or user.force_password_change:
            raise ValueError("Unavailable account")
        membership = database.scalar(
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.user_id == user.id,
                OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            )
            .with_for_update()
        )
        if membership is None:
            raise ValueError("Unavailable membership")
        identity = database.scalar(
            select(SamlIdentity).where(
                SamlIdentity.configuration_id == configuration.id, SamlIdentity.subject == subject
            )
        )
        existing = database.scalar(
            select(SamlIdentity).where(
                SamlIdentity.configuration_id == configuration.id, SamlIdentity.user_id == user.id
            )
        )
        if (identity is not None and identity.user_id != user.id) or (
            existing is not None and existing.subject != subject
        ):
            raise ValueError("Conflicting identity")
        if identity is None:
            database.add(
                SamlIdentity(configuration_id=configuration.id, user_id=user.id, subject=subject)
            )
        database.add(
            SamlAssertionReplay(
                assertion_id=assertion_id,
                organization_id=organization_id,
                configuration_id=configuration.id,
                expires_at=expiry + timedelta(seconds=CLOCK_SKEW),
            )
        )
        transaction.consumed_at = datetime.now(UTC)
        database.flush()  # Unique assertion/subject constraints reject concurrent replays.
        if transaction.return_path == FRONTEND_RETURN_PATH:
            login_code = secrets.token_urlsafe(48)
            database.add(
                OAuthLoginCode(
                    user_id=user.id,
                    sso_organization_id=organization_id,
                    code_hash=hash_reset_token(login_code),
                    expires_at=datetime.now(UTC)
                    + timedelta(seconds=settings.google_oauth_login_code_expire_seconds),
                )
            )
            database.commit()
            logger.info("saml_authentication_succeeded")
            return _frontend_login_redirect(code=login_code)
        token = issue_access_token(database, user, sso_organization_id=organization_id)
        database.commit()
        logger.info("saml_authentication_succeeded")
        return AccessTokenResponse(access_token=token)
    except (ValueError, IntegrityError):
        database.rollback()
        logger.warning("saml_authentication_failed")
        if transaction is not None and transaction.return_path == FRONTEND_RETURN_PATH:
            return _frontend_login_redirect(error="failed")
        raise HTTPException(400, FAILURE) from None
    except HTTPException:
        database.rollback()
        if transaction is not None and transaction.return_path == FRONTEND_RETURN_PATH:
            return _frontend_login_redirect(error="failed")
        raise
    except Exception:
        database.rollback()
        logger.warning("saml_authentication_failed")
        if transaction is not None and transaction.return_path == FRONTEND_RETURN_PATH:
            return _frontend_login_redirect(error="failed")
        raise HTTPException(400, FAILURE) from None


@router.post("/exchange", response_model=SamlLoginResponse)
def exchange_saml_login_code(
    payload: SamlLoginCodeRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> SamlLoginResponse:
    limiter.check(
        "saml-login-exchange",
        client_identifier(request),
        settings.saml_rate_limit,
        settings.saml_rate_window_seconds,
        fail_closed=True,
    )
    now = datetime.now(UTC)
    login_code = database.scalar(
        select(OAuthLoginCode)
        .where(OAuthLoginCode.code_hash == hash_reset_token(payload.code))
        .with_for_update()
    )
    if (
        login_code is None
        or login_code.sso_organization_id is None
        or login_code.used_at is not None
        or _utc(login_code.expires_at) <= now
    ):
        raise HTTPException(400, "SAML sign-in session is invalid or expired.")
    user = database.get(User, login_code.user_id)
    organization = database.get(Organization, login_code.sso_organization_id)
    membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.user_id == login_code.user_id,
            OrganizationMember.organization_id == login_code.sso_organization_id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
        )
    )
    if (
        user is None
        or not user.is_active
        or user.force_password_change
        or organization is None
        or organization.status is not OrganizationStatus.ACTIVE
        or membership is None
    ):
        login_code.used_at = now
        database.commit()
        raise HTTPException(401, "Unable to complete SAML sign-in.")
    login_code.used_at = now
    token = issue_access_token(database, user, sso_organization_id=login_code.sso_organization_id)
    database.commit()
    return SamlLoginResponse(
        access_token=token, organization_id=str(login_code.sso_organization_id)
    )
