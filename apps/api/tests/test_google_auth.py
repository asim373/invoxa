import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session

from apps.api.app.database import get_db
from apps.api.app.google_auth import GoogleIdentity
from apps.api.app.main import app
from apps.api.app.models import OAuthIdentity, OAuthLoginCode, User, UserRole
from apps.api.app.settings import settings


@pytest.fixture
def google_configuration(monkeypatch):
    monkeypatch.setattr(settings, "google_oauth_client_id", "google-client-id")
    monkeypatch.setattr(settings, "google_oauth_client_secret", SecretStr("google-client-secret"))
    monkeypatch.setattr(
        settings, "google_oauth_redirect_uri", "http://testserver/auth/google/callback"
    )
    monkeypatch.setattr(settings, "frontend_base_url", "http://frontend.test")


def test_google_start_uses_state_nonce_pkce_and_secure_transaction_cookie(
    google_configuration,
) -> None:
    client = TestClient(app)
    response = client.get("/auth/google/start", follow_redirects=False)

    assert response.status_code == 302
    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["client_id"] == ["google-client-id"]
    assert query["state"][0]
    assert query["nonce"][0]
    assert query["code_challenge_method"] == ["S256"]
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/auth/google" in cookie


def test_google_start_is_unavailable_without_credentials(monkeypatch) -> None:
    monkeypatch.setattr(settings, "google_oauth_client_id", None)
    monkeypatch.setattr(settings, "google_oauth_client_secret", None)
    response = TestClient(app).get("/auth/google/start")
    assert response.status_code == 503
    assert response.json() == {"detail": "Google Sign-In is not configured."}


def test_google_identity_validation_rejects_wrong_nonce_and_unverified_email(
    google_configuration, monkeypatch
) -> None:
    from apps.api.app.google_auth import verify_google_id_token

    signing_key = MagicMock()
    signing_key.key = "public-key"
    jwks_client = MagicMock()
    jwks_client.get_signing_key_from_jwt.return_value = signing_key
    monkeypatch.setattr(
        "apps.api.app.google_auth.jwt.PyJWKClient", lambda *args, **kwargs: jwks_client
    )
    claims = {
        "exp": 9999999999,
        "iat": 1,
        "iss": "https://accounts.google.com",
        "sub": "durable-google-subject",
        "email": "verified@example.com",
        "email_verified": True,
        "nonce": "expected-nonce",
    }
    monkeypatch.setattr("apps.api.app.google_auth.jwt.decode", lambda *args, **kwargs: claims)
    assert (
        verify_google_id_token("signed-token", "expected-nonce").subject == "durable-google-subject"
    )

    with pytest.raises(jwt.InvalidTokenError):
        verify_google_id_token("signed-token", "wrong-nonce")
    claims["nonce"] = "expected-nonce"
    claims["email_verified"] = False
    with pytest.raises(jwt.InvalidTokenError):
        verify_google_id_token("signed-token", "expected-nonce")


def test_google_callback_links_verified_existing_user_and_issues_one_time_code(
    google_configuration, monkeypatch
) -> None:
    existing_user = User(
        id=uuid.uuid4(),
        email="person@example.com",
        password_hash="existing-password-hash",
        auth_version=3,
        is_active=True,
        role=UserRole.ADMIN,
    )
    database = MagicMock(spec=Session)
    database.scalar.side_effect = [None, existing_user]
    added: list[object] = []
    database.add.side_effect = added.append
    app.dependency_overrides[get_db] = lambda: database
    monkeypatch.setattr(
        "apps.api.app.google_auth.exchange_google_code", lambda code, verifier: "verified-id-token"
    )
    monkeypatch.setattr(
        "apps.api.app.google_auth.verify_google_id_token",
        lambda token, nonce: GoogleIdentity(
            subject="google-subject-123", email="person@example.com"
        ),
    )
    client = TestClient(app)
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]

    callback = client.get(
        f"/auth/google/callback?state={state}&code=provider-code", follow_redirects=False
    )
    app.dependency_overrides.clear()

    assert callback.status_code == 303
    assert "google_code=" in callback.headers["location"]
    identity = next(value for value in added if isinstance(value, OAuthIdentity))
    login_code = next(value for value in added if isinstance(value, OAuthLoginCode))
    assert identity.user_id == existing_user.id
    assert identity.subject == "google-subject-123"
    assert login_code.user_id == existing_user.id
    database.commit.assert_called_once()


def test_google_callback_creates_new_reviewer_without_usable_local_password(
    google_configuration, monkeypatch
) -> None:
    database = MagicMock(spec=Session)
    database.scalar.side_effect = [None, None]
    added: list[object] = []

    def add(value: object) -> None:
        added.append(value)

    def flush() -> None:
        user = next(value for value in added if isinstance(value, User))
        user.id = uuid.uuid4()
        user.is_active = True
        user.auth_version = 0

    database.add.side_effect = add
    database.flush.side_effect = flush
    app.dependency_overrides[get_db] = lambda: database
    monkeypatch.setattr(
        "apps.api.app.google_auth.exchange_google_code", lambda code, verifier: "verified-id-token"
    )
    monkeypatch.setattr(
        "apps.api.app.google_auth.verify_google_id_token",
        lambda token, nonce: GoogleIdentity(subject="new-google-subject", email="new@example.com"),
    )
    client = TestClient(app)
    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    response = client.get(
        f"/auth/google/callback?state={state}&code=provider-code", follow_redirects=False
    )
    app.dependency_overrides.clear()

    assert response.status_code == 303
    user = next(value for value in added if isinstance(value, User))
    assert user.email == "new@example.com"
    assert user.role == UserRole.REVIEWER
    assert user.password_hash != ""
    assert any(isinstance(value, OAuthIdentity) for value in added)


def test_google_callback_rejects_invalid_state_and_provider_failure(
    google_configuration, monkeypatch
) -> None:
    client = TestClient(app)
    invalid_state = client.get(
        "/auth/google/callback?state=forged&code=provider-code", follow_redirects=False
    )
    assert "google_error=invalid_state" in invalid_state.headers["location"]

    start = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    monkeypatch.setattr(
        "apps.api.app.google_auth.exchange_google_code",
        lambda code, verifier: (_ for _ in ()).throw(jwt.ExpiredSignatureError()),
    )
    database = MagicMock(spec=Session)
    app.dependency_overrides[get_db] = lambda: database
    failed = client.get(f"/auth/google/callback?state={state}&code=expired", follow_redirects=False)
    app.dependency_overrides.clear()
    assert "google_error=failed" in failed.headers["location"]
    database.rollback.assert_called_once()


def test_google_login_code_is_single_use_and_preserves_auth_version(
    google_configuration,
) -> None:
    user = User(
        id=uuid.uuid4(),
        email="google@example.com",
        password_hash="unused",
        auth_version=4,
        is_active=True,
        role=UserRole.REVIEWER,
    )
    login_code = OAuthLoginCode(
        user_id=user.id,
        code_hash="stored-hash",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    database = MagicMock(spec=Session)
    database.scalar.return_value = login_code
    database.get.return_value = user
    app.dependency_overrides[get_db] = lambda: database
    client = TestClient(app)
    raw_code = "a" * 48
    monkeypatch_hash = pytest.MonkeyPatch()
    monkeypatch_hash.setattr(
        "apps.api.app.google_auth.hash_reset_token", lambda value: "stored-hash"
    )

    accepted = client.post("/auth/google/exchange", json={"code": raw_code})
    replayed = client.post("/auth/google/exchange", json={"code": raw_code})
    monkeypatch_hash.undo()
    app.dependency_overrides.clear()

    assert accepted.status_code == 200
    claims = jwt.decode(
        accepted.json()["access_token"],
        settings.auth_secret_key.get_secret_value(),
        algorithms=["HS256"],
    )
    assert claims["ver"] == 4
    assert replayed.status_code == 400


def test_google_login_code_rejects_inactive_account(google_configuration) -> None:
    user = User(
        id=uuid.uuid4(),
        email="disabled@example.com",
        password_hash="unused",
        auth_version=0,
        is_active=False,
        role=UserRole.REVIEWER,
    )
    login_code = OAuthLoginCode(
        user_id=user.id,
        code_hash="stored-hash",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    database = MagicMock(spec=Session)
    database.scalar.return_value = login_code
    database.get.return_value = user
    app.dependency_overrides[get_db] = lambda: database
    response = TestClient(app).post("/auth/google/exchange", json={"code": "b" * 48})
    app.dependency_overrides.clear()
    assert response.status_code == 401
