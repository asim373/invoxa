from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.auth import get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    Document,
    DocumentStatus,
    InvoiceExtraction,
    InvoiceLineItem,
    User,
)


@pytest.fixture
def productivity_client():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    owner = User(email="productivity@example.com", password_hash="unused", auth_version=0)
    other = User(email="other-productivity@example.com", password_hash="unused", auth_version=0)
    with session_factory() as database:
        database.add_all([owner, other])
        database.commit()
        database.refresh(owner)
        database.refresh(other)
        now = datetime.now(UTC)
        alpha = Document(
            owner_id=owner.id,
            original_filename="alpha-invoice.pdf",
            mime_type="application/pdf",
            file_size=100,
            checksum="a" * 64,
            storage_location="alpha.pdf",
            status=DocumentStatus.COMPLETED,
            created_at=now - timedelta(days=1),
            updated_at=now,
        )
        beta = Document(
            owner_id=owner.id,
            original_filename="beta-scan.png",
            mime_type="image/png",
            file_size=200,
            checksum="b" * 64,
            storage_location="beta.png",
            status=DocumentStatus.COMPLETED,
            created_at=now,
            updated_at=now,
        )
        private = Document(
            owner_id=other.id,
            original_filename="private-vendor.pdf",
            mime_type="application/pdf",
            file_size=300,
            checksum="c" * 64,
            storage_location="private.pdf",
            status=DocumentStatus.COMPLETED,
            created_at=now,
            updated_at=now,
        )
        database.add_all([alpha, beta, private])
        database.flush()
        extraction = InvoiceExtraction(
            document_id=alpha.id,
            invoice_number="INV-ALPHA",
            invoice_date=date(2026, 8, 1),
            vendor_name="Acme Services",
            customer_name="Northwind Customer",
            currency="USD",
            subtotal=Decimal("100.00"),
            tax=Decimal("20.00"),
            total=Decimal("120.00"),
            is_valid=True,
        )
        database.add(extraction)
        database.flush()
        database.add(
            InvoiceLineItem(
                invoice_document_id=alpha.id,
                position=0,
                description="Consulting",
                quantity=Decimal("1"),
                unit_price=Decimal("100"),
                line_total=Decimal("100"),
            )
        )
        database.commit()

    def database_override():
        with session_factory() as database:
            yield database

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = lambda: owner
    try:
        with TestClient(app) as client:
            yield client, alpha, beta, private
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("term", ["alpha-invoice", "INV-ALPHA", "Acme", "Northwind"])
def test_search_matches_owned_document_fields(productivity_client, term: str) -> None:
    client, alpha, _, _ = productivity_client
    response = client.get("/documents", params={"search": term})
    assert response.status_code == 200
    assert [item["id"] for item in response.json()["items"]] == [str(alpha.id)]


def test_search_filter_sort_and_pagination_preserve_isolation(productivity_client) -> None:
    client, alpha, beta, private = productivity_client
    assert client.get("/documents", params={"search": "private-vendor"}).json()["items"] == []
    filtered = client.get("/documents", params={"mime_type": "image/png"}).json()
    assert [item["id"] for item in filtered["items"]] == [str(beta.id)]
    sorted_page = client.get(
        "/documents",
        params={"sort_by": "filename", "sort_direction": "asc", "page_size": 1, "page": 1},
    ).json()
    assert sorted_page["total"] == 2
    assert sorted_page["total_pages"] == 2
    assert sorted_page["items"][0]["id"] == str(alpha.id)
    assert str(private.id) not in str(sorted_page)


def test_invalid_search_and_sort_are_rejected(productivity_client) -> None:
    client, *_ = productivity_client
    assert client.get("/documents", params={"search": ""}).status_code == 422
    assert client.get("/documents", params={"sort_by": "database_column"}).status_code == 422
    assert client.get("/documents", params={"sort_direction": "sideways"}).status_code == 422


def test_json_export_is_structured_download_and_supports_owned_bulk(productivity_client) -> None:
    client, alpha, beta, _ = productivity_client
    single = client.get(f"/documents/{alpha.id}/exports/json")
    assert single.status_code == 200
    assert single.headers["content-type"].startswith("application/json")
    assert single.headers["content-disposition"].endswith('.json"')
    payload = single.json()
    assert payload[0]["invoice_number"] == "INV-ALPHA"
    assert payload[0]["vendor_name"] == "Acme Services"
    assert payload[0]["total"] == "120.00"
    assert payload[0]["line_items"] == [
        {
            "line_item_index": 0,
            "description": "Consulting",
            "quantity": "1.0000",
            "unit_price": "100.00",
            "line_total": "100.00",
        }
    ]
    assert "storage_location" not in str(payload)
    bulk = client.post("/exports/json", json={"document_ids": [str(alpha.id), str(beta.id)]})
    assert bulk.status_code == 200
    assert {entry["document_id"] for entry in bulk.json()} == {str(alpha.id), str(beta.id)}


def test_json_export_rejects_empty_or_mixed_ownership(productivity_client) -> None:
    client, alpha, _, private = productivity_client
    assert client.post("/exports/json", json={"document_ids": []}).status_code == 422
    denied = client.post("/exports/json", json={"document_ids": [str(alpha.id), str(private.id)]})
    assert denied.status_code == 404
