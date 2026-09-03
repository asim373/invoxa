import logging

import pytest
import redis
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

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


def test_production_settings_reject_default_secret_and_wildcard_cors() -> None:
    with pytest.raises(ValueError, match="AUTH_SECRET_KEY"):
        Settings(app_environment="production", database_url="postgresql://safe", cors_origins=[])
    with pytest.raises(ValueError, match="Wildcard CORS"):
        Settings(
            app_environment="production",
            database_url="postgresql://safe",
            auth_secret_key=SecretStr("a-secure-production-secret-that-is-long-enough"),
            cors_origins=["*"],
        )


def test_production_settings_reject_weak_secret_and_wildcard_hosts() -> None:
    common = {
        "app_environment": "production",
        "database_url": "postgresql://safe",
        "cors_origins": ["https://app.example.com"],
        "frontend_base_url": "https://app.example.com",
        "smtp_host": "smtp.example.com",
        "smtp_from_address": "no-reply@example.com",
    }
    with pytest.raises(ValueError, match="AUTH_SECRET_KEY"):
        Settings(**common, auth_secret_key=SecretStr("too-short"))
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        Settings(
            **common,
            auth_secret_key=SecretStr("a-secure-production-secret-that-is-long-enough"),
            allowed_hosts=["*"],
        )


def test_api_responses_include_minimal_security_headers() -> None:
    response = TestClient(app).get("/health")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
