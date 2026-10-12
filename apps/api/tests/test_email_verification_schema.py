"""Disposable schema checks; never connects to the configured PostgreSQL database."""

import hashlib
import importlib
from datetime import UTC, datetime, timedelta

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from apps.api.app.database import Base
from apps.api.app.models import EmailVerificationToken, User


def test_verification_model_preserves_legacy_default_and_unique_hash():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        user = User(email="legacy@example.com", password_hash="unused")
        db.add(user)
        db.flush()
        assert user.email_verified is True
        digest = hashlib.sha256(b"disposable-test-token").hexdigest()
        db.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=digest,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
        db.add(
            EmailVerificationToken(
                user_id=user.id,
                token_hash=digest,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
    engine.dispose()


def test_migration_preserves_existing_accounts_and_rolls_back():
    migration = importlib.import_module("apps.api.migrations.versions.0031_email_verification")
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id CHAR(32) PRIMARY KEY, email TEXT)"))
        connection.execute(text("INSERT INTO users VALUES ('legacy', 'legacy@example.com')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT email_verified FROM users")).scalar() == 1
            assert "email_verification_tokens" in inspect(connection).get_table_names()
            migration.downgrade()
        assert connection.execute(text("SELECT email FROM users")).scalar() == "legacy@example.com"
        assert "email_verification_tokens" not in inspect(connection).get_table_names()
    engine.dispose()
