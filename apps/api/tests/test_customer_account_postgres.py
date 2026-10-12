"""Two opt-in checks using only explicitly disposable PostgreSQL storage."""

import hashlib
import importlib
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from apps.api.app.customer_account import router
from apps.api.app.database import Base, get_db
from apps.api.app.models import EmailChangeToken, User
from apps.api.app.rate_limit import get_rate_limiter


@pytest.fixture(scope="module")
def engine():
    url = os.environ.get("PHASE8_POSTGRES_TEST_URL")
    if not url:
        pytest.skip("Disposable PostgreSQL URL required")
    parsed = make_url(url)
    assert parsed.host == "127.0.0.1" and parsed.database == "phase8_disposable"
    value = create_engine(url)
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def test_additive_customer_schema_preserves_user(engine):
    with Session(engine) as db:
        user = User(email="legacy-account@example.com", password_hash="unused")
        db.add(user)
        db.commit()
        identifier = user.id
    migration = importlib.import_module("apps.api.migrations.versions.0033_customer_account")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            migration.upgrade()
        assert inspect(connection).has_table("email_change_tokens")
    with Session(engine) as db:
        assert db.get(User, identifier).email == "legacy-account@example.com"


def test_concurrent_email_confirmation_consumes_once(engine):
    raw = "disposable-test-token-" + "x" * 48
    with Session(engine) as db:
        user = User(email="concurrent-old@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        db.add(
            EmailChangeToken(
                user_id=user.id,
                old_email=user.email,
                new_email="concurrent-new@example.com",
                auth_version=0,
                token_hash=hashlib.sha256(raw.encode()).hexdigest(),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        db.commit()
    application = FastAPI()
    application.include_router(router)

    def session():
        with Session(engine) as db:
            yield db

    class Limiter:
        def check(self, *args, **kwargs):
            pass

    application.dependency_overrides[get_db] = session
    application.dependency_overrides[get_rate_limiter] = lambda: Limiter()

    def confirm():
        with TestClient(application) as client:
            return client.post(
                "/auth/account/email-change/confirm", json={"token": raw}
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm(), range(2)))
    assert sorted(results) == [200, 400]
    with Session(engine) as db:
        user = db.scalar(select(User).where(User.email == "concurrent-new@example.com"))
        assert user.auth_version == 1
