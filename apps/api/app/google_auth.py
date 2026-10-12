import base64
import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, EmailStr, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from apps.api.app.auth import (
    AccessTokenResponse,
    hash_password,
    hash_reset_token,
    issue_access_token,
    normalize_email,
)
from apps.api.app.database import get_db
from apps.api.app.models import (
    OAuthIdentity,
    OAuthLoginCode,
    Organization,
    OrganizationInvitation,
    OrganizationInvitationStatus,
    OrganizationMember,
    OrganizationMemberStatus,
    User,
    UserRole,
)
from apps.api.app.provisioning import provision_company
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.seat_quota import require_active_seat
from apps.api.app.settings import settings

router = APIRouter(prefix="/auth/google", tags=["authentication"])
logger = logging.getLogger(__name__)

GOOGLE_AUTHORIZATION_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}
TRANSACTION_COOKIE = "invoxa_google_oauth"


class GoogleConfigurationResponse(BaseModel):
    enabled: bool


class GoogleLoginCodeRequest(BaseModel):
    code: str = Field(min_length=32, max_length=256)


class GoogleOnboardingRequest(BaseModel):
    onboarding_token: str = Field(min_length=32, max_length=2048)
    company_name: str = Field(min_length=1, max_length=255)


class GoogleOnboardingResponse(BaseModel):
    google_code: str
    organization_id: str
    organization_name: str


class GoogleIdentity(BaseModel):
    subject: str
    email: EmailStr


def google_is_configured() -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


def create_google_onboarding_token(identity: GoogleIdentity) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "kind": "google_company_onboarding",
            "sub": identity.subject,
            "email": normalize_email(str(identity.email)),
            "jti": secrets.token_urlsafe(24),
            "iat": now,
            "exp": now + timedelta(minutes=10),
        },
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
    )


def read_google_onboarding_token(token: str) -> GoogleIdentity:
    payload = jwt.decode(
        token,
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
        options={"require": ["kind", "sub", "email", "jti", "exp"]},
    )
    if payload["kind"] != "google_company_onboarding":
        raise jwt.InvalidTokenError("Invalid onboarding token kind")
    return GoogleIdentity(subject=str(payload["sub"]), email=str(payload["email"]))


def _frontend_redirect(
    error: str | None = None,
    code: str | None = None,
    onboarding: str | None = None,
    google_invitation: bool = False,
    organization_id: str | None = None,
) -> RedirectResponse:
    query = urlencode(
        {
            key: value
            for key, value in {
                "google_error": error,
                "google_code": code,
                "google_onboarding": onboarding,
                "google_invitation": "1" if google_invitation else None,
                "organization_id": organization_id,
            }.items()
            if value
        }
    )
    separator = "&" if "?" in settings.frontend_base_url else "?"
    url = settings.frontend_base_url.rstrip("/")
    if query:
        url = f"{url}/{separator}{query}"
    response = RedirectResponse(url=url, status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(TRANSACTION_COOKIE, path="/auth/google")
    return response


def _transaction_token(
    state_value: str, nonce: str, verifier: str, invitation_token: str | None = None
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "kind": "google_oauth_transaction",
            "state": state_value,
            "nonce": nonce,
            "verifier": verifier,
            "invitation_token": invitation_token,
            "iat": now,
            "exp": now + timedelta(seconds=settings.google_oauth_transaction_expire_seconds),
        },
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
    )


def _read_transaction(token: str, returned_state: str) -> tuple[str, str, str | None]:
    try:
        payload = jwt.decode(
            token,
            settings.auth_secret_key.get_secret_value(),
            algorithms=["HS256"],
            options={"require": ["kind", "state", "nonce", "verifier", "iat", "exp"]},
        )
        if payload["kind"] != "google_oauth_transaction" or not secrets.compare_digest(
            str(payload["state"]), returned_state
        ):
            raise ValueError
        invitation_token = payload.get("invitation_token")
        return (
            str(payload["nonce"]),
            str(payload["verifier"]),
            (str(invitation_token) if invitation_token else None),
        )
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        raise ValueError("invalid OAuth transaction") from None


def exchange_google_code(code: str, verifier: str) -> str:
    client_secret = settings.google_oauth_client_secret
    if not settings.google_oauth_client_id or client_secret is None:
        raise RuntimeError("Google authentication is not configured")
    response = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": settings.google_oauth_client_id,
            "client_secret": client_secret.get_secret_value(),
            "redirect_uri": settings.google_oauth_redirect_uri,
            "code_verifier": verifier,
        },
        timeout=10,
    )
    response.raise_for_status()
    token_response = response.json()
    if not isinstance(token_response, dict):
        raise ValueError("Invalid Google token response")
    token = token_response.get("id_token")
    if not isinstance(token, str) or not token:
        raise ValueError("Google response did not contain an identity token")
    return token


def verify_google_id_token(id_token: str, nonce: str) -> GoogleIdentity:
    if not settings.google_oauth_client_id:
        raise RuntimeError("Google authentication is not configured")
    jwks_client = jwt.PyJWKClient(GOOGLE_JWKS_URL, cache_keys=True)
    signing_key = jwks_client.get_signing_key_from_jwt(id_token)
    claims = jwt.decode(
        id_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=settings.google_oauth_client_id,
        options={"require": ["exp", "iat", "iss", "sub", "email", "email_verified", "nonce"]},
    )
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise jwt.InvalidIssuerError("Untrusted Google issuer")
    if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise jwt.InvalidTokenError("Invalid nonce")
    if claims.get("email_verified") is not True:
        raise jwt.InvalidTokenError("Google email is not verified")
    authorized_party = claims.get("azp")
    if authorized_party and authorized_party != settings.google_oauth_client_id:
        raise jwt.InvalidAudienceError("Invalid authorized party")
    subject = str(claims["sub"])
    email = normalize_email(str(claims["email"]))
    if not subject or len(subject) > 255:
        raise jwt.InvalidTokenError("Invalid subject")
    return GoogleIdentity(subject=subject, email=email)


@router.get("/config", response_model=GoogleConfigurationResponse)
def google_configuration() -> GoogleConfigurationResponse:
    return GoogleConfigurationResponse(enabled=google_is_configured())


@router.get("/start")
def start_google_authentication(
    request: Request,
    invitation_token: str | None = Query(default=None, min_length=32, max_length=256),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> RedirectResponse:
    if not google_is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Google Sign-In is not configured.",
        )
    limiter.check(
        "google-auth-start",
        client_identifier(request),
        settings.google_oauth_rate_limit,
        settings.google_oauth_rate_window_seconds,
        fail_closed=True,
    )
    state_value = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    query = urlencode(
        {
            "client_id": settings.google_oauth_client_id,
            "redirect_uri": settings.google_oauth_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state_value,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    response = RedirectResponse(f"{GOOGLE_AUTHORIZATION_URL}?{query}", status_code=302)
    response.set_cookie(
        TRANSACTION_COOKIE,
        _transaction_token(state_value, nonce, verifier, invitation_token),
        max_age=settings.google_oauth_transaction_expire_seconds,
        httponly=True,
        secure=settings.is_production_like,
        samesite="lax",
        path="/auth/google",
    )
    return response


@router.get("/callback")
def google_callback(
    request: Request,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> RedirectResponse:
    limiter.check(
        "google-auth-callback",
        client_identifier(request),
        settings.google_oauth_rate_limit,
        settings.google_oauth_rate_window_seconds,
        fail_closed=True,
    )
    if error or not state or not code:
        return _frontend_redirect(error="cancelled" if error == "access_denied" else "failed")
    transaction = request.cookies.get(TRANSACTION_COOKIE)
    if not transaction:
        return _frontend_redirect(error="invalid_state")
    try:
        nonce, verifier, invitation_token = _read_transaction(transaction, state)
        identity_data = verify_google_id_token(exchange_google_code(code, verifier), nonce)
        if invitation_token is not None:
            raw_login_code, organization_id = _accept_google_invitation(
                database, invitation_token, identity_data
            )
            database.commit()
            return _frontend_redirect(
                code=raw_login_code,
                google_invitation=True,
                organization_id=str(organization_id),
            )
        identity = database.scalar(
            select(OAuthIdentity).where(
                OAuthIdentity.provider == "google", OAuthIdentity.subject == identity_data.subject
            )
        )
        new_user = False
        if identity is not None:
            user = database.get(User, identity.user_id)
        else:
            user = database.scalar(select(User).where(User.email == identity_data.email))
            if user is None:
                new_user = True
            if user is not None and not user.is_active:
                return _frontend_redirect(error="account_unavailable")
            if user is not None:
                database.add(
                    OAuthIdentity(
                        user_id=user.id,
                        provider="google",
                        subject=identity_data.subject,
                    )
                )
        if new_user:
            return _frontend_redirect(onboarding=create_google_onboarding_token(identity_data))
        if user is None or not user.is_active:
            return _frontend_redirect(error="account_unavailable")
        user.email_verified = True
        raw_login_code = secrets.token_urlsafe(48)
        database.add(
            OAuthLoginCode(
                user_id=user.id,
                code_hash=hash_reset_token(raw_login_code),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.google_oauth_login_code_expire_seconds),
            )
        )
        database.commit()
        logger.info("google_authentication_succeeded")
        return _frontend_redirect(code=raw_login_code)
    except ValueError as exc:
        database.rollback()
        if "email does not match" in str(exc):
            return _frontend_redirect(error="invitation_email_mismatch", google_invitation=True)
        if "invitation" in str(exc) or "organization" in str(exc):
            return _frontend_redirect(error="invitation_invalid", google_invitation=True)
        return _frontend_redirect(error="failed")
    except (httpx.HTTPError, jwt.PyJWTError, SQLAlchemyError, ValidationError):
        database.rollback()
        logger.warning("google_authentication_failed", exc_info=True)
        return _frontend_redirect(error="failed")


def _accept_google_invitation(
    database: Session, raw_token: str, identity: GoogleIdentity
) -> tuple[str, uuid.UUID]:
    invitation = database.scalar(
        select(OrganizationInvitation)
        .where(OrganizationInvitation.token_hash == hash_reset_token(raw_token))
        .with_for_update()
    )
    if invitation is None or invitation.status is not OrganizationInvitationStatus.PENDING:
        raise ValueError("invitation is no longer available")
    expires_at = invitation.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        invitation.status = OrganizationInvitationStatus.EXPIRED
        raise ValueError("invitation is expired")
    organization = database.get(Organization, invitation.organization_id)
    if organization is None or organization.status.value != "active":
        raise ValueError("organization is inactive")
    if normalize_email(str(identity.email)) != invitation.invited_email:
        raise ValueError("invitation email does not match Google account")

    oauth_identity = database.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == "google", OAuthIdentity.subject == identity.subject
        )
    )
    user = database.get(User, oauth_identity.user_id) if oauth_identity is not None else None
    if user is None:
        user = database.scalar(select(User).where(User.email == invitation.invited_email))
    if user is None:
        user = User(
            email=invitation.invited_email,
            password_hash=hash_password(secrets.token_urlsafe(48)),
            role=UserRole.REVIEWER,
        )
        database.add(user)
        database.flush()
    if normalize_email(user.email) != invitation.invited_email:
        raise ValueError("invitation email does not match Google account")
    if oauth_identity is None:
        database.add(OAuthIdentity(user_id=user.id, provider="google", subject=identity.subject))
    if not user.is_active:
        raise ValueError("account is unavailable")
    membership = database.scalar(
        select(OrganizationMember).where(
            OrganizationMember.organization_id == invitation.organization_id,
            OrganizationMember.user_id == user.id,
        )
    )
    if membership is not None:
        if membership.status is OrganizationMemberStatus.ACTIVE:
            raise ValueError("user is already a member")
        require_active_seat(database, organization)
        membership.status = OrganizationMemberStatus.ACTIVE
        membership.role = invitation.role
        membership.joined_at = datetime.now(UTC)
    else:
        require_active_seat(database, organization)
        database.add(
            OrganizationMember(
                organization_id=invitation.organization_id,
                user_id=user.id,
                role=invitation.role,
                status=OrganizationMemberStatus.ACTIVE,
                joined_at=datetime.now(UTC),
            )
        )
    invitation.status = OrganizationInvitationStatus.ACCEPTED
    invitation.accepted_at = datetime.now(UTC)
    raw_login_code = secrets.token_urlsafe(48)
    database.add(
        OAuthLoginCode(
            user_id=user.id,
            code_hash=hash_reset_token(raw_login_code),
            expires_at=datetime.now(UTC)
            + timedelta(seconds=settings.google_oauth_login_code_expire_seconds),
        )
    )
    return raw_login_code, invitation.organization_id


@router.post("/complete-onboarding", response_model=GoogleOnboardingResponse)
def complete_google_onboarding(
    payload: GoogleOnboardingRequest,
    database: Session = Depends(get_db),  # noqa: B008
) -> GoogleOnboardingResponse:
    try:
        identity_data = read_google_onboarding_token(payload.onboarding_token)
    except (jwt.InvalidTokenError, ValidationError, ValueError):
        raise HTTPException(
            status_code=400,
            detail="Google onboarding session is invalid or expired.",
        ) from None

    existing_identity = database.scalar(
        select(OAuthIdentity).where(
            OAuthIdentity.provider == "google",
            OAuthIdentity.subject == identity_data.subject,
        )
    )
    existing_user = database.scalar(select(User).where(User.email == identity_data.email))
    if existing_identity is not None or existing_user is not None:
        raise HTTPException(status_code=409, detail="Google onboarding has already been completed.")

    user = User(
        email=normalize_email(str(identity_data.email)),
        password_hash=hash_password(secrets.token_urlsafe(48)),
        role=UserRole.REVIEWER,
    )
    try:
        database.add(user)
        database.flush()
        database.add(
            OAuthIdentity(
                user_id=user.id,
                provider="google",
                subject=identity_data.subject,
            )
        )
        organization = provision_company(database, user=user, company_name=payload.company_name)
        raw_login_code = secrets.token_urlsafe(48)
        database.add(
            OAuthLoginCode(
                user_id=user.id,
                code_hash=hash_reset_token(raw_login_code),
                expires_at=datetime.now(UTC)
                + timedelta(seconds=settings.google_oauth_login_code_expire_seconds),
            )
        )
        database.commit()
    except ValueError as exc:
        database.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except SQLAlchemyError:
        database.rollback()
        raise HTTPException(
            status_code=409,
            detail="Google onboarding could not be completed.",
        ) from None

    return GoogleOnboardingResponse(
        google_code=raw_login_code,
        organization_id=str(organization.id),
        organization_name=organization.name,
    )


@router.post("/exchange", response_model=AccessTokenResponse)
def exchange_google_login_code(
    payload: GoogleLoginCodeRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> AccessTokenResponse:
    limiter.check(
        "google-auth-exchange",
        client_identifier(request),
        settings.google_oauth_rate_limit,
        settings.google_oauth_rate_window_seconds,
        fail_closed=True,
    )
    now = datetime.now(UTC)
    login_code = database.scalar(
        select(OAuthLoginCode)
        .where(OAuthLoginCode.code_hash == hash_reset_token(payload.code))
        .with_for_update()
    )
    expires_at = login_code.expires_at if login_code else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        login_code is None
        or getattr(login_code, "sso_organization_id", None) is not None
        or login_code.used_at is not None
        or expires_at <= now
    ):
        raise HTTPException(status_code=400, detail="Google sign-in session is invalid or expired.")
    user = database.get(User, login_code.user_id)
    if user is None or not user.is_active:
        login_code.used_at = now
        database.commit()
        raise HTTPException(status_code=401, detail="Unable to sign in with Google.")
    login_code.used_at = now
    access_token = issue_access_token(database, user)
    database.commit()
    return AccessTokenResponse(access_token=access_token)
