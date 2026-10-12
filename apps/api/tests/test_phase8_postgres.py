"""Opt-in tests against an explicitly disposable PostgreSQL database only."""

import importlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from apps.api.app.database import Base
from apps.api.app.email_outbox import enqueue, process_email
from apps.api.app.models import TransactionalEmail, User


@pytest.fixture(scope="module")
def engine():
    raw = os.environ.get("PHASE8_POSTGRES_TEST_URL")
    if not raw:
        pytest.skip("Explicit disposable PostgreSQL URL required")
    url = make_url(raw)
    assert url.host == "127.0.0.1" and url.database == "phase8_disposable"
    value = create_engine(url)
    Base.metadata.create_all(value)
    yield value
    value.dispose()


def test_postgres_additive_migrations_preserve_legacy_user(engine):
    verification = importlib.import_module("apps.api.migrations.versions.0031_email_verification")
    outbox = importlib.import_module("apps.api.migrations.versions.0032_transactional_email_outbox")
    with Session(engine) as db:
        user = User(email="legacy-postgres@example.com", password_hash="unused")
        db.add(user)
        db.commit()
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            outbox.downgrade()
            verification.downgrade()
            verification.upgrade()
            outbox.upgrade()
        assert connection.execute(text("SELECT email_verified FROM users")).scalar() is True
        indexes = {row["name"] for row in inspect(connection).get_indexes("transactional_emails")}
        assert "ix_transactional_emails_due" in indexes


def mail(subject):
    value = EmailMessage()
    value["From"] = "sender@example.com"
    value["To"] = "recipient@example.com"
    value["Subject"] = subject
    value.set_content("Disposable concurrency test")
    return value


def test_two_workers_claim_different_deliveries(engine):
    with Session(engine) as db:
        enqueue(db, mail("Concurrent A"), "test")
        enqueue(db, mail("Concurrent B"), "test")
        db.commit()
    barrier = threading.Barrier(2)
    observed = []
    lock = threading.Lock()

    def deliver(message, kind):
        with lock:
            observed.append(message["Subject"])
        barrier.wait(timeout=15)

    def worker():
        with Session(engine) as db:
            return process_email(db, deliver)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: worker(), range(2)))
    assert results == [True, True]
    assert sorted(observed) == ["Concurrent A", "Concurrent B"]
    with Session(engine) as db:
        rows = list(db.scalars(select(TransactionalEmail)))
        assert len(rows) == 2 and all(row.status == "sent" and row.attempts == 1 for row in rows)


def test_postgres_reclaims_expired_worker_lease(engine):
    with Session(engine) as db:
        row = enqueue(db, mail("Abandoned worker"), "test")
        row.status = "sending"
        row.attempts = 1
        row.lease_until = datetime.now(UTC) - timedelta(seconds=1)
        db.commit()
        assert process_email(db, lambda *args: None)
        db.refresh(row)
        assert row.status == "sent" and row.attempts == 2


def test_concurrent_identical_enqueue_is_idempotent(engine):
    barrier = threading.Barrier(2)

    def create():
        with Session(engine) as db:
            barrier.wait(timeout=15)
            row = enqueue(db, mail("Idempotent concurrent enqueue"), "test")
            db.commit()
            return row.id

    with ThreadPoolExecutor(max_workers=2) as pool:
        identifiers = list(pool.map(lambda _: create(), range(2)))
    assert identifiers[0] == identifiers[1]
