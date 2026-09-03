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
from apps.api.app.email import PasswordResetEmailSender, get_password_reset_email_sender
from apps.api.app.models import PasswordResetToken, User, UserRole
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
    is_active: bool
    role: UserRole
    created_at: datetime
    updated_at: datetime

    @field_validator("role", mode="before")
    @classmethod
    def default_legacy_role(cls, value: UserRole | None) -> UserRole:
        return value or UserRole.REVIEWER


class AccessTokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


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


def create_access_token(user_id: uuid.UUID, auth_version: int = 0) -> str:
    issued_at = datetime.now(UTC)
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.auth_access_token_expire_minutes)
    return jwt.encode(
        {"sub": str(user_id), "iat": issued_at, "exp": expires_at, "ver": auth_version},
        settings.auth_secret_key.get_secret_value(),
        algorithm="HS256",
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
    except (jwt.InvalidTokenError, KeyError, TypeError, ValueError):
        logger.warning("authentication_failed reason=invalid_token")
        raise _unauthorized() from None

    user = database.get(User, user_id)
    if user is None or not user.is_active or token_auth_version != (user.auth_version or 0):
        logger.warning("authentication_failed reason=inactive_or_missing_user")
        raise _unauthorized()
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


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
def register_user(
    payload: RegistrationRequest,
    request: Request,
    database: Session = Depends(get_db),  # noqa: B008
    limiter: RateLimiter = Depends(get_rate_limiter),  # noqa: B008
) -> User:
    email = normalize_email(str(payload.email))
    limiter.check(
        "registration",
        client_identifier(request),
        settings.registration_rate_limit,
        settings.registration_rate_window_seconds,
    )
    existing_user = database.scalar(select(User).where(User.email == email))
    if existing_user is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered."
        )

    user = User(email=email, password_hash=hash_password(payload.password), role=UserRole.REVIEWER)
    database.add(user)
    try:
        database.commit()
        database.refresh(user)
    except IntegrityError:
        database.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email is already registered."
        ) from None
    logger.info("user_registered")
    return user


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
    logger.info("authentication_succeeded")
    return AccessTokenResponse(access_token=create_access_token(user.id, user.auth_version or 0))


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
            logger.info("password_reset_requested delivery=accepted")
        except Exception:
            logger.exception("password_reset_delivery_failed")
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
    user.auth_version = (user.auth_version or 0) + 1
    token_record.used_at = now
    database.commit()
    logger.info("password_reset_completed")
    return MessageResponse(detail="Your password has been reset. You can now sign in.")


@router.get("/me", response_model=UserResponse)
def get_me(
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> User:
    return current_user
