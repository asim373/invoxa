import logging
from typing import Any, cast

import pytest
import redis
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from apps.api.app import auth as auth_module
from apps.api.app.main import app
from apps.api.app.rate_limit import RateLimiter
from apps.api.app.settings import Settings, settings


class CountingRedis:
    def __init__(self) -> None:
        self.count = 0

    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        self.count += 1
        return self.count


class UnavailableRedis:
    def eval(self, script: str, numkeys: int, *keys_and_args: object) -> int:
        raise redis.ConnectionError("unavailable")


def test_rate_limiter_blocks_after_configured_limit(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    limiter = RateLimiter(CountingRedis())

    limiter.check("login", "client:user", limit=2, window_seconds=60)
    limiter.check("login", "client:user", limit=2, window_seconds=60)
    with pytest.raises(HTTPException) as captured:
        limiter.check("login", "client:user", limit=2, window_seconds=60)

    assert captured.value.status_code == 429
    assert captured.value.detail == "Too many requests. Please try again later."


def test_rate_limiter_fails_open_without_leaking_identifier(monkeypatch, caplog) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    secret_identifier = "private-user@example.com"
    with caplog.at_level(logging.ERROR):
        RateLimiter(UnavailableRedis()).check("login", secret_identifier, 1, 60)

    assert secret_identifier not in caplog.text
    assert "rate_limit_backend_unavailable scope=login" in caplog.text


def test_sensitive_rate_limiter_fails_closed_with_controlled_response(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    with pytest.raises(HTTPException) as captured:
        RateLimiter(UnavailableRedis()).check("login", "opaque-id", 1, 60, fail_closed=True)
    assert captured.value.status_code == 503
    assert captured.value.headers is not None
    assert captured.value.headers["Retry-After"] == "60"
    assert captured.value.detail == "Request protection is temporarily unavailable."


def test_login_fails_closed_when_redis_is_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    app.dependency_overrides[auth_module.get_rate_limiter] = lambda: RateLimiter(UnavailableRedis())
    try:
        response = TestClient(app).post(
            "/auth/login", json={"email": "nobody@example.com", "password": "synthetic-test"}
        )
    finally:
        app.dependency_overrides.pop(auth_module.get_rate_limiter, None)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "300"
    assert "unavailable" in response.json()["detail"].lower()


def isolated_settings(**values):
    settings_type = cast(Any, Settings)
    return settings_type(_env_file=None, **values)


def production_settings(**overrides):
    values = {
        "app_environment": "production",
        "database_url": "postgresql://app:secret@postgres:5432/invoxa",
        "redis_url": "redis://redis:6379/0",
        "auth_secret_key": SecretStr("a-secure-production-secret-that-is-long-enough"),
        "cors_origins": ["https://app.example.com"],
        "allowed_hosts": ["api.example.com"],
        "rate_limit_enabled": True,
        "frontend_base_url": "https://app.example.com",
        "smtp_host": "smtp.example.com",
        "smtp_from_address": "no-reply@example.com",
        "smtp_starttls": True,
        "smtp_use_ssl": False,
        "saml_public_base_url": "https://api.example.com",
        "scim_public_base_url": "https://api.example.com",
    }
    values.update(overrides)
    return isolated_settings(**values)


def test_production_settings_reject_default_secret_and_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="AUTH_SECRET_KEY"):
        production_settings(
            auth_secret_key=SecretStr("change-me-before-production-use-a-random-secret")
        )
    with pytest.raises(ValueError, match="Wildcard CORS"):
        production_settings(cors_origins=["*"])
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        production_settings(cors_origins=["https://app.example.com/"])


def test_production_settings_reject_weak_secret_and_wildcard_hosts() -> None:
    common = {
        "app_environment": "production",
        "database_url": "postgresql://app:secret@postgres:5432/invoxa",
        "redis_url": "redis://redis:6379/0",
        "auth_secret_key": SecretStr("a-secure-production-secret-that-is-long-enough"),
        "cors_origins": ["https://app.example.com"],
        "allowed_hosts": ["api.example.com"],
        "rate_limit_enabled": True,
        "frontend_base_url": "https://app.example.com",
        "smtp_host": "smtp.example.com",
        "smtp_from_address": "no-reply@example.com",
        "saml_public_base_url": "https://api.example.com",
        "scim_public_base_url": "https://api.example.com",
    }
    with pytest.raises(ValueError, match="AUTH_SECRET_KEY"):
        isolated_settings(**{**common, "auth_secret_key": SecretStr("too-short")})
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        isolated_settings(**{**common, "allowed_hosts": ["localhost"]})


def test_production_settings_require_https_cors_and_rate_limiting() -> None:
    common = {
        "app_environment": "production",
        "database_url": "postgresql://app:secret@postgres:5432/invoxa",
        "redis_url": "redis://redis:6379/0",
        "auth_secret_key": SecretStr("a-secure-production-secret-that-is-long-enough"),
        "frontend_base_url": "https://app.example.com",
        "allowed_hosts": ["api.example.com"],
        "smtp_host": "smtp.example.com",
        "smtp_from_address": "no-reply@example.com",
        "saml_public_base_url": "https://api.example.com",
        "scim_public_base_url": "https://api.example.com",
    }
    with pytest.raises(ValueError, match="CORS_ORIGINS"):
        isolated_settings(
            **{**common, "cors_origins": ["http://app.example.com"], "rate_limit_enabled": True}
        )
    with pytest.raises(ValueError, match="RATE_LIMIT_ENABLED"):
        isolated_settings(
            **{**common, "cors_origins": ["https://app.example.com"], "rate_limit_enabled": False}
        )


@pytest.mark.parametrize("environment", ["production", "staging"])
def test_production_like_settings_reject_local_dependencies_and_missing_sso_urls(environment):
    with pytest.raises(ValueError, match="REDIS_URL"):
        production_settings(app_environment=environment, redis_url="redis://localhost:6379/0")
    with pytest.raises(ValueError, match="SAML_PUBLIC_BASE_URL"):
        production_settings(app_environment=environment, saml_public_base_url=None)
    with pytest.raises(ValueError, match="SCIM_PUBLIC_BASE_URL"):
        production_settings(
            app_environment=environment, scim_public_base_url="http://api.example.com"
        )


def test_production_like_settings_reject_unsafe_frontend_saml_and_smtp_values():
    with pytest.raises(ValueError, match="FRONTEND_BASE_URL"):
        production_settings(frontend_base_url="http://app.example.com")
    with pytest.raises(ValueError, match="SAML_PUBLIC_BASE_URL"):
        production_settings(saml_public_base_url="https://localhost")
    with pytest.raises(ValueError, match="SCIM_PUBLIC_BASE_URL"):
        production_settings(scim_public_base_url="https://api.example.com/scim/v2")
    with pytest.raises(ValueError, match="SMTP_USERNAME"):
        production_settings(smtp_username="smtp-user")
    with pytest.raises(ValueError, match="SMTP_PORT"):
        production_settings(smtp_port=70000)
    with pytest.raises(ValueError, match="SMTP_FROM_ADDRESS"):
        production_settings(smtp_from_address="not-an-email")


def test_configuration_errors_hide_database_credentials():
    with pytest.raises(ValueError) as captured:
        production_settings(
            database_url="postgresql://app:synthetic-db-secret@postgres:5432/invoxa",
            cors_origins=["http://app.example.com"],
        )
    assert "synthetic-db-secret" not in str(captured.value)


def test_app_environment_rejects_typo_and_worker_does_not_require_api_delivery_settings():
    with pytest.raises(ValueError, match="APP_ENVIRONMENT"):
        isolated_settings(app_environment="prodution")
    worker = production_settings(
        app_component="worker",
        cors_origins=["http://localhost:5173"],
        allowed_hosts=["localhost"],
        rate_limit_enabled=False,
        frontend_base_url="http://localhost:5173",
        smtp_host=None,
        smtp_from_address=None,
        saml_public_base_url=None,
        scim_public_base_url=None,
    )
    assert worker.is_production_like


def test_psycopg_health_url_normalizes_sqlalchemy_driver_qualifier():
    configured = production_settings(database_url="postgresql+psycopg://app:secret@db:5432/invoxa")
    assert configured.psycopg_database_url == "postgresql://app:secret@db:5432/invoxa"


def test_api_responses_include_minimal_security_headers() -> None:
    response = TestClient(app).get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"


def test_production_api_responses_include_hsts(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_environment", "production")

    response = TestClient(app).get("/health")

    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"


def test_staging_api_responses_include_hsts(monkeypatch) -> None:
    monkeypatch.setattr(settings, "app_environment", "staging")
    response = TestClient(app).get("/health")
    assert response.headers["strict-transport-security"] == "max-age=31536000; includeSubDomains"
