from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import (
    INVALID_RESET_TOKEN_DETAIL,
    PASSWORD_RESET_REQUEST_DETAIL,
    create_access_token,
    hash_password,
    hash_reset_token,
    verify_password,
)
from apps.api.app.database import Base, get_db
from apps.api.app.email import get_password_reset_email_sender
from apps.api.app.main import app
from apps.api.app.models import PasswordResetToken, User
from apps.api.app.rate_limit import get_rate_limiter


class CapturingEmailSender:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def send_password_reset(self, recipient: str, reset_url: str) -> None:
        self.messages.append((recipient, reset_url))


@pytest.fixture
def recovery_client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    sender = CapturingEmailSender()

    def database_override():
        with session_factory() as database:
            yield database

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_password_reset_email_sender] = lambda: sender
    try:
        with TestClient(app) as client:
            yield client, session_factory, sender
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def create_user(session_factory, email: str, password: str = "OriginalPassword123!") -> User:
    with session_factory() as database:
        user = User(email=email, password_hash=hash_password(password), auth_version=0)
        database.add(user)
        database.commit()
        database.refresh(user)
        return user


def request_token(client: TestClient, sender: CapturingEmailSender, email: str) -> str:
    response = client.post("/auth/forgot-password", json={"email": email})
    assert response.status_code == 200
    assert response.json() == {"detail": PASSWORD_RESET_REQUEST_DETAIL}
    return parse_qs(urlparse(sender.messages[-1][1]).query)["token"][0]


def test_forgot_password_is_generic_and_only_delivers_for_existing_active_user(
    recovery_client,
) -> None:
    client, session_factory, sender = recovery_client
    create_user(session_factory, "known@example.com")

    known = client.post("/auth/forgot-password", json={"email": "KNOWN@example.com"})
    unknown = client.post("/auth/forgot-password", json={"email": "missing@example.com"})

    assert known.json() == unknown.json() == {"detail": PASSWORD_RESET_REQUEST_DETAIL}
    assert len(sender.messages) == 1
    raw_token = parse_qs(urlparse(sender.messages[0][1]).query)["token"][0]
    with session_factory() as database:
        stored = database.scalar(select(PasswordResetToken))
        assert stored is not None
        assert stored.token_hash == hash_reset_token(raw_token)
        assert raw_token != stored.token_hash


def test_valid_reset_changes_hashed_password_invalidates_session_and_is_one_time(
    recovery_client,
) -> None:
    client, session_factory, sender = recovery_client
    user = create_user(session_factory, "reset@example.com")
    old_access_token = create_access_token(user.id, 0)
    raw_token = request_token(client, sender, user.email)

    reset = client.post(
        "/auth/reset-password",
        json={"token": raw_token, "new_password": "ReplacementPassword123!"},
    )
    reused = client.post(
        "/auth/reset-password",
        json={"token": raw_token, "new_password": "AnotherPassword123!"},
    )

    assert reset.status_code == 200
    assert reused.status_code == 400
    assert reused.json() == {"detail": INVALID_RESET_TOKEN_DETAIL}
    old_session = client.get("/auth/me", headers={"Authorization": f"Bearer {old_access_token}"})
    assert old_session.status_code == 401
    assert (
        client.post(
            "/auth/login", json={"email": user.email, "password": "OriginalPassword123!"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/auth/login", json={"email": user.email, "password": "ReplacementPassword123!"}
        ).status_code
        == 200
    )
    with session_factory() as database:
        changed_user = database.get(User, user.id)
        stored = database.scalar(select(PasswordResetToken))
        assert changed_user is not None and stored is not None
        assert changed_user.password_hash != "ReplacementPassword123!"
        assert verify_password("ReplacementPassword123!", changed_user.password_hash)
        assert changed_user.auth_version == 1
        assert stored.used_at is not None


@pytest.mark.parametrize("token", ["x" * 32, "malformed-token-that-is-long-enough"])
def test_incorrect_or_malformed_reset_token_is_rejected(recovery_client, token: str) -> None:
    client, _, _ = recovery_client
    response = client.post(
        "/auth/reset-password",
        json={"token": token, "new_password": "ReplacementPassword123!"},
    )
    assert response.status_code == 400
    assert response.json() == {"detail": INVALID_RESET_TOKEN_DETAIL}
    assert token not in response.text


def test_expired_token_and_weak_password_are_rejected(recovery_client) -> None:
    client, session_factory, sender = recovery_client
    user = create_user(session_factory, "expired@example.com")
    raw_token = request_token(client, sender, user.email)
    with session_factory() as database:
        stored = database.scalar(select(PasswordResetToken))
        assert stored is not None
        stored.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        database.commit()

    expired = client.post(
        "/auth/reset-password",
        json={"token": raw_token, "new_password": "ReplacementPassword123!"},
    )
    weak = client.post("/auth/reset-password", json={"token": "x" * 32, "new_password": "short"})
    assert expired.status_code == 400
    assert weak.status_code == 422


def test_reset_token_cannot_change_another_users_password(recovery_client) -> None:
    client, session_factory, sender = recovery_client
    owner = create_user(session_factory, "owner-reset@example.com")
    other = create_user(session_factory, "other-reset@example.com")
    raw_token = request_token(client, sender, owner.email)
    response = client.post(
        "/auth/reset-password",
        json={"token": raw_token, "new_password": "OwnerReplacement123!"},
    )
    assert response.status_code == 200
    with session_factory() as database:
        unchanged_other = database.get(User, other.id)
        assert unchanged_other is not None
        assert verify_password("OriginalPassword123!", unchanged_other.password_hash)


def test_new_request_supersedes_previous_active_token(recovery_client) -> None:
    client, session_factory, sender = recovery_client
    user = create_user(session_factory, "superseded@example.com")
    first_token = request_token(client, sender, user.email)
    second_token = request_token(client, sender, user.email)

    rejected = client.post(
        "/auth/reset-password",
        json={"token": first_token, "new_password": "ReplacementPassword123!"},
    )
    accepted = client.post(
        "/auth/reset-password",
        json={"token": second_token, "new_password": "ReplacementPassword123!"},
    )
    assert rejected.status_code == 400
    assert accepted.status_code == 200


def test_recovery_endpoints_use_rate_limiter(recovery_client) -> None:
    client, _, _ = recovery_client

    class RejectingLimiter:
        def check(self, scope: str, identifier: str, limit: int, window_seconds: int) -> None:
            raise HTTPException(status_code=429, detail="Too many requests.")

    app.dependency_overrides[get_rate_limiter] = lambda: RejectingLimiter()
    forgot = client.post("/auth/forgot-password", json={"email": "anyone@example.com"})
    reset = client.post(
        "/auth/reset-password",
        json={"token": "x" * 32, "new_password": "ReplacementPassword123!"},
    )
    assert forgot.status_code == reset.status_code == 429
