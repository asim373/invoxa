import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.database import get_db
from apps.api.app.email import (
    PasswordResetEmailSender,
    VerificationEmailSender,
    get_password_reset_email_sender,
    get_verification_email_sender,
)
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    OrganizationStatus,
    PasswordResetToken,
    User,
    UserRole,
    UserSession,
)
from apps.api.app.provisioning import provision_company
from apps.api.app.rate_limit import RateLimiter, client_identifier, get_rate_limiter
from apps.api.app.settings import settings

router = APIRouter(prefix="/auth", tags=["authentication"])
bearer_scheme = HTTPBearer(auto_error=False)
password_hasher = PasswordHash.recommended()
INVALID_CREDENTIALS_DETAIL = "Invalid email or password."
PASSWORD_RESET_REQUEST_DETAIL = (
    "If an account exists for this email, password reset instructions have been sent."
)
INVALID_RESET_TOKEN_DETAIL = "This password reset link is invalid or has expired."
DUMMY_PASSWORD_HASH = password_hasher.hash("not-a-user-password")
logger = logging.getLogger(__name__)


def normalize_email(email: str) -> str:
    return email.strip().casefold()


class RegistrationRequest(BaseModel):
    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=12, max_length=128)
    company_name: str = Field(min_length=1, max_length=255)

    @field_validator("password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        return validate_password(value)


class LoginRequest(BaseModel):
    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr = Field(max_length=320)


class PasswordResetRequest(BaseModel):
    token: str = Field(min_length=32, max_length=256)
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        return validate_password(value)


class MessageResponse(BaseModel):
    detail: str


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    email_verified: bool = True
    is_active: bool
    role: UserRole
    created_at: datetime
    updated_at: datetime
    organization_id: uuid.UUID | None = None
    organization_name: str | None = None
    membership_role: OrganizationMemberRole | None = None
    force_password_change: bool = False

    @field_validator("force_password_change", mode="before")
    @classmethod
    def default_missing_force_password_change(cls, value: bool | None) -> bool:
        return bool(value)


class RegistrationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    email_verified: bool = True
    is_active: bool
    role: UserRole
    created_at: datetime
    updated_at: datetime
    organization_id: uuid.UUID
    organization_name: str
    membership_role: OrganizationMemberRole
    force_password_change: bool = False

    @field_validator("force_password_change", mode="before")
    @classmethod
    def default_missing_force_password_change(cls, value: bool | None) -> bool:
        return bool(value)

    @field_validator("role", mode="before")
    @classmethod
    def default_legacy_role(cls, value: UserRole | None) -> UserRole:
        return value or UserRole.REVIEWER


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    password_change_required: bool = False

    @field_validator("password_change_required", mode="before")
    @classmethod
    def default_missing_password_change_required(cls, value: bool | None) -> bool:
        return bool(value)


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def validate_password(password: str) -> str:
    if not password.strip():
        raise ValueError("Password must not be blank.")
    return password


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_password(password: str, password_hash: str) -> bool:
    return password_hasher.verify(password, password_hash)


def create_access_token(
    user_id: uuid.UUID,
    auth_version: int = 0,
    *,
    session_id: uuid.UUID | None = None,
    sso_organization_id: uuid.UUID | None = None,
) -> str:
    issued_at = datetime.now(UTC)
    expires_at = issued_at + timedelta(minutes=settings.auth_access_token_expire_minutes)
    session_id = session_id or uuid.uuid4()
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": issued_at,
            "exp": expires_at,
            "ver": auth_version,
            "sid": str(session_id),
            "sso_org": str(sso_organization_id) if sso_organization_id else None,
        },
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
    )


def issue_access_token(
    database: Session, user: User, *, sso_organization_id: uuid.UUID | None = None
) -> str:
    now = datetime.now(UTC)
    session_id = uuid.uuid4()
    expiry = now + timedelta(minutes=settings.auth_access_token_expire_minutes)
    database.add(
        UserSession(
            id=session_id,
            user_id=user.id,
            auth_version=user.auth_version or 0,
            sso_organization_id=sso_organization_id,
            created_at=now,
            last_seen_at=now,
            expires_at=expiry,
        )
    )
    database.flush()
    return create_access_token(
        user.id,
        user.auth_version or 0,
        session_id=session_id,
        sso_organization_id=sso_organization_id,
    )


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated.",
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        logger.warning("authentication_failed reason=missing_credentials")
        raise _unauthorized()
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.auth_secret_key.get_secret_value(),
            algorithms=["HS256"],
        )
        user_id = uuid.UUID(payload["sub"])
        token_auth_version = int(payload["ver"])
        session_id = uuid.UUID(payload["sid"])
        sso_org = uuid.UUID(payload["sso_org"]) if payload.get("sso_org") else None
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        logger.warning("authentication_failed reason=invalid_token")
        raise _unauthorized() from None

    user = database.get(User, user_id)
    if user is None or not user.is_active or token_auth_version != (user.auth_version or 0):
        logger.warning("authentication_failed reason=inactive_or_missing_user")
        raise _unauthorized()
    if not user.email_verified:
        raise HTTPException(403, "Verify your email address before accessing your account.")
    session = database.get(UserSession, session_id)
    now = datetime.now(UTC)
    if session is None:
        if settings.is_production_like:
            raise _unauthorized()
        # Supports tokens created by the existing internal helper while ensuring
        # every non-production test/helper session becomes revocable before use.
        session = UserSession(
            id=session_id,
            user_id=user.id,
            auth_version=token_auth_version,
            sso_organization_id=sso_org,
            expires_at=now + timedelta(minutes=settings.auth_access_token_expire_minutes),
            created_at=now,
            last_seen_at=now,
        )
        database.add(session)
        database.commit()
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if (
        session.user_id != user.id
        or session.revoked_at is not None
        or expires_at <= now
        or session.auth_version != token_auth_version
        or session.sso_organization_id != sso_org
    ):
        raise _unauthorized()
    last_seen = session.last_seen_at
    if last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=UTC)
    if last_seen < now - timedelta(minutes=5):
        session.last_seen_at = now
        database.commit()
    setattr(user, "_current_session_id", session.id)  # noqa: B010
    setattr(user, "_current_session_sso_organization_id", session.sso_organization_id)  # noqa: B010
    return user


def get_current_editor(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> User:
    # Persisted users always have a role; the fallback supports pre-migration test objects.
    role = current_user.role or UserRole.REVIEWER
    if role not in {UserRole.ADMIN, UserRole.REVIEWER}:
        logger.warning("authorization_denied action=document_mutation role=%s", role.value)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to perform this action.",
        )
    return current_user


@router.post("/register", response_model=RegistrationResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: RegistrationRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    email_sender: VerificationEmailSender = Depends(get_verification_email_sender),  # noqa: B008
) -> RegistrationResponse:
    email = normalize_email(str(payload.email))
    limiter.check(
        "registration",
        client_identifier(request),
        settings.registration_rate_limit,
        settings.registration_rate_window_seconds,
        fail_closed=True,
    )
    existing_user = database.scalar(select(User).where(User.email == email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered."
        )

    user = User(email=email, password_hash=hash_password(payload.password),
                role=UserRole.REVIEWER, email_verified=False)
    database.add(user)
    try:
        database.flush()
        organization = provision_company(database, user=user, company_name=payload.company_name)
        from apps.api.app.email_verification import issue_verification

        try:
            issue_verification(database, user, email_sender)
        except Exception:
            database.rollback()
            raise HTTPException(503, "Verification delivery is temporarily unavailable.") from None
        database.commit()
        database.refresh(user)
    except ValueError as exc:
        database.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except IntegrityError:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered."
        ) from None
    logger.info("user_registered")
    return RegistrationResponse(
        **UserResponse.model_validate(user).model_dump(
            exclude={"organization_id", "organization_name", "membership_role"}
        ),
        organization_id=organization.id,
        organization_name=organization.name,
        membership_role=OrganizationMemberRole.OWNER,
    )


@router.post("/login", response_model=AccessTokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> AccessTokenResponse:
    email = normalize_email(str(payload.email))
    limiter.check(
        "login",
        f"{client_identifier(request)}:{email}",
        settings.login_rate_limit,
        settings.login_rate_window_seconds,
        fail_closed=True,
    )
    user = database.scalar(select(User).where(User.email == email))
    if user is None:
        verify_password(payload.password, DUMMY_PASSWORD_HASH)
        logger.warning("authentication_failed reason=invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS_DETAIL
        )
    password_is_valid = verify_password(payload.password, user.password_hash)
    if not user.is_active or not password_is_valid:
        logger.warning("authentication_failed reason=invalid_credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS_DETAIL
        )
    if not user.email_verified:
        raise HTTPException(403, "Verify your email address before signing in.")
    logger.info("authentication_succeeded")
    token = issue_access_token(database, user)
    database.commit()
    return AccessTokenResponse(
        access_token=token,
        password_change_required=user.force_password_change,
    )


class ChangePasswordRequest(BaseModel):
    new_password: str = Field(min_length=12, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_must_not_be_blank(cls, value: str) -> str:
        return validate_password(value)


@router.post("/change-password", response_model=MessageResponse)
def change_authenticated_password(
    payload: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),  # noqa: B008
    database: Session = Depends(get_db),  # noqa: B008
) -> MessageResponse:
    if not current_user.force_password_change:
        raise HTTPException(status_code=400, detail="A password change is not required.")
    current_user.password_hash = hash_password(payload.new_password)
    current_user.force_password_change = False
    current_user.auth_version = (current_user.auth_version or 0) + 1
    database.execute(
        update(UserSession)
        .where(UserSession.user_id == current_user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=datetime.now(UTC))
    )
    database.commit()
    from apps.api.app.security_notifications import audit_account, notify

    audit_account(database, current_user, "password_changed")
    notify(database, current_user, "password_changed")
    database.commit()
    return MessageResponse(detail="Your password has been changed. Please sign in again.")


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(
    payload: ForgotPasswordRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
    email_sender: PasswordResetEmailSender = Depends(get_password_reset_email_sender),  # noqa: B008
) -> MessageResponse:
    limiter.check(
        "forgot-password",
        client_identifier(request),
        settings.forgot_password_rate_limit,
        settings.forgot_password_rate_window_seconds,
        fail_closed=True,
    )
    email = normalize_email(str(payload.email))
    user = database.scalar(select(User).where(User.email == email))
    if user is not None and user.is_active:
        now = datetime.now(UTC)
        database.execute(
            update(PasswordResetToken)
            .where(
                PasswordResetToken.user_id == user.id,
                PasswordResetToken.used_at.is_(None),
            )
            .values(used_at=now)
        )
        raw_token = secrets.token_urlsafe(48)
        reset_token = PasswordResetToken(
            user_id=user.id,
            token_hash=hash_reset_token(raw_token),
            expires_at=now + timedelta(minutes=settings.password_reset_token_expire_minutes),
        )
        database.add(reset_token)
        database.commit()
        reset_query = urlencode({"token": raw_token})
        reset_url = f"{settings.frontend_base_url.rstrip('/')}/reset-password?{reset_query}"
        try:
            email_sender.send_password_reset(user.email, reset_url)
            database.commit()
            logger.info("password_reset_requested delivery=accepted")
        except Exception:
            database.rollback()
            reset_token.used_at = datetime.now(UTC)
            database.commit()
            logger.warning("password_reset_delivery_failed")
    else:
        # Deliberately perform comparable token work without persisting or sending anything.
        hash_reset_token(secrets.token_urlsafe(48))
        logger.info("password_reset_requested delivery=suppressed")
    return MessageResponse(detail=PASSWORD_RESET_REQUEST_DETAIL)


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(
    payload: PasswordResetRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> MessageResponse:
    limiter.check(
        "reset-password",
        client_identifier(request),
        settings.reset_password_rate_limit,
        settings.reset_password_rate_window_seconds,
        fail_closed=True,
    )
    now = datetime.now(UTC)
    token_record = database.scalar(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hash_reset_token(payload.token))
        .with_for_update()
    )
    expires_at = token_record.expires_at if token_record is not None else now
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if token_record is None or token_record.used_at is not None or expires_at <= now:
        logger.warning("password_reset_token_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_RESET_TOKEN_DETAIL
        )
    user = database.get(User, token_record.user_id)
    if user is None or not user.is_active:
        token_record.used_at = now
        database.commit()
        logger.warning("password_reset_token_rejected")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=INVALID_RESET_TOKEN_DETAIL
        )

    user.password_hash = hash_password(payload.new_password)
    user.force_password_change = False
    user.auth_version = (user.auth_version or 0) + 1
    database.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )
    token_record.used_at = now
    database.commit()
    logger.info("password_reset_completed")
    from apps.api.app.security_notifications import audit_account, notify

    audit_account(database, user, "password_reset_completed")
    notify(database, user, "password_reset", event_id=token_record.id)
    database.commit()
    return MessageResponse(detail="Your password has been reset. You can now sign in.")


@router.get("/me", response_model=UserResponse)
def get_me(
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> UserResponse:
    if current_user.role is None:
        current_user.role = UserRole.REVIEWER
    membership = database.scalar(
        select(OrganizationMember)
        .join(Organization, Organization.id == OrganizationMember.organization_id)
        .where(
            OrganizationMember.user_id == current_user.id,
            OrganizationMember.status == OrganizationMemberStatus.ACTIVE,
            Organization.status == OrganizationStatus.ACTIVE,
        )
        .order_by(OrganizationMember.joined_at, OrganizationMember.id)
    )
    response = UserResponse.model_validate(current_user)
    if membership is None:
        return response
    organization = database.get(Organization, membership.organization_id)
    return response.model_copy(
        update={
            "organization_id": membership.organization_id,
            "organization_name": organization.name if organization else None,
            "membership_role": membership.role,
        }
    )
