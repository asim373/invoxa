import json
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from apps.api.app.ai_analysis import analyze_document, generate_finding_specs
from apps.api.app.auth import get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    AIAnalysisStatus,
    AIFinding,
    AIFindingCategory,
    AIFindingStatus,
    Document,
    DocumentStatus,
    InvoiceExtraction,
    InvoiceLineItem,
    User,
    UserRole,
)


@pytest.fixture
def phase9a_database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def add_user(database: Session, email: str, role: UserRole = UserRole.REVIEWER) -> User:
    user = User(email=email, password_hash="unused", role=role, auth_version=0)
    database.add(user)
    database.flush()
    return user


def add_invoice(
    database: Session,
    owner: User,
    *,
    number: str | None = "INV-1",
    vendor: str | None = "Acme",
    invoice_date: date | None = None,
    currency: str | None = "USD",
    subtotal: str | None = "100.00",
    tax: str | None = "10.00",
    total: str | None = "110.00",
    checksum: str | None = None,
    created_at: datetime | None = None,
    valid: bool = True,
) -> Document:
    created = created_at or datetime.now(UTC)
    document = Document(
        owner_id=owner.id,
        original_filename=f"{uuid.uuid4()}.pdf",
        mime_type="application/pdf",
        file_size=100,
        checksum=checksum or uuid.uuid4().hex.ljust(64, "0"),
        storage_location=f"{uuid.uuid4()}.pdf",
        status=DocumentStatus.COMPLETED,
        ai_analysis_status=AIAnalysisStatus.PENDING,
        created_at=created,
        updated_at=created,
    )
    database.add(document)
    database.flush()
    extraction = InvoiceExtraction(
        document_id=document.id,
        invoice_number=number,
        invoice_date=invoice_date or created.date(),
        vendor_name=vendor,
        currency=currency,
        subtotal=Decimal(subtotal) if subtotal is not None else None,
        tax=Decimal(tax) if tax is not None else None,
        total=Decimal(total) if total is not None else None,
        is_valid=valid,
        validation_error=None if valid else "Extracted totals do not reconcile.",
    )
    document.invoice_extraction = extraction
    database.flush()
    return document


def add_line(document: Document, quantity: str, price: str, total: str) -> None:
    assert document.invoice_extraction is not None
    document.invoice_extraction.line_items.append(
        InvoiceLineItem(
            invoice_document_id=document.id,
            position=len(document.invoice_extraction.line_items),
            description="Professional services",
            quantity=Decimal(quantity),
            unit_price=Decimal(price),
            line_total=Decimal(total),
        )
    )


def test_arithmetic_missing_date_tax_and_low_confidence_findings(phase9a_database) -> None:
    with phase9a_database() as database:
        owner = add_user(database, "analysis@example.com")
        document = add_invoice(
            database,
            owner,
            number=None,
            vendor=None,
            invoice_date=datetime.now(UTC).date() + timedelta(days=5),
            subtotal="100.00",
            tax="-5.00",
            total="150.00",
            valid=False,
        )
        add_line(document, "2", "25", "70")
        categories = {finding.category for finding in generate_finding_specs(database, document)}
        assert {
            AIFindingCategory.ARITHMETIC,
            AIFindingCategory.MISSING,
            AIFindingCategory.DATE,
            AIFindingCategory.TAX,
            AIFindingCategory.CONFIDENCE,
        } <= categories


def test_exact_and_probable_duplicate_use_multiple_signals(phase9a_database) -> None:
    with phase9a_database() as database:
        owner = add_user(database, "duplicates@example.com")
        original = add_invoice(database, owner, checksum="a" * 64)
        exact = add_invoice(database, owner, number="OTHER", checksum="a" * 64)
        exact_specs = generate_finding_specs(database, exact)
        assert any(spec.title == "Exact duplicate document detected" for spec in exact_specs)

        original_extraction = original.invoice_extraction
        assert original_extraction is not None
        probable = add_invoice(
            database,
            owner,
            number=original_extraction.invoice_number,
            vendor=original_extraction.vendor_name,
            invoice_date=original_extraction.invoice_date,
            total="999.00",
        )
        probable_specs = generate_finding_specs(database, probable)
        finding = next(
            spec for spec in probable_specs if spec.category == AIFindingCategory.DUPLICATE
        )
        assert finding.title == "Possible duplicate invoice"
        assert finding.confidence == Decimal("0.8000")


def test_amount_anomaly_requires_sufficient_history(phase9a_database) -> None:
    with phase9a_database() as database:
        owner = add_user(database, "history@example.com")
        for index in range(4):
            add_invoice(database, owner, number=f"INV-{index}", total="100.00")
        small_sample = add_invoice(database, owner, number="SMALL", total="1000.00")
        assert not any(
            finding.category == AIFindingCategory.AMOUNT
            for finding in generate_finding_specs(database, small_sample)
        )
        add_invoice(database, owner, number="FIFTH", total="100.00")
        adequate = add_invoice(database, owner, number="OUTLIER", total="1000.00")
        finding = next(
            finding
            for finding in generate_finding_specs(database, adequate)
            if finding.category == AIFindingCategory.AMOUNT
        )
        sample_size = finding.evidence["sample_size"]
        assert isinstance(sample_size, int)
        assert sample_size >= 5
        assert finding.evidence["method"] == "median absolute deviation"


def test_analysis_is_idempotent_and_preserves_review_state(phase9a_database) -> None:
    with phase9a_database() as database:
        owner = add_user(database, "idempotent@example.com")
        document = add_invoice(database, owner, number=None)
        analyze_document(database, document)
        database.commit()
        first = database.scalars(select(AIFinding)).all()
        assert len(first) == 1
        first[0].status = AIFindingStatus.ACKNOWLEDGED
        database.commit()
        analyze_document(database, document)
        database.commit()
        second = database.scalars(select(AIFinding)).all()
        assert len(second) == 1
        assert second[0].status == AIFindingStatus.ACKNOWLEDGED
        assert document.ai_analysis_status == AIAnalysisStatus.COMPLETED


@pytest.fixture
def phase9a_client(phase9a_database):
    factory = phase9a_database
    with factory() as database:
        owner = add_user(database, "owner-phase9a@example.com")
        other = add_user(database, "other-phase9a@example.com")
        viewer = add_user(database, "viewer-phase9a@example.com", UserRole.VIEWER)
        first = add_invoice(database, owner, number=None, vendor="Acme", currency="USD")
        add_invoice(
            database, owner, number="EUR-1", vendor="Euro Vendor", currency="EUR", total="50.00"
        )
        private = add_invoice(database, other, number=None, vendor="Private Vendor")
        analyze_document(database, first)
        analyze_document(database, private)
        database.commit()

    current = {"user": owner}

    def database_override():
        with factory() as database:
            yield database

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_current_user] = lambda: current["user"]
    try:
        with TestClient(app) as client:
            yield client, current, owner, other, viewer, first, private
    finally:
        app.dependency_overrides.clear()


def test_finding_review_resolve_reopen_and_role_control(phase9a_client) -> None:
    client, current, owner, _, viewer, _, _ = phase9a_client
    finding = client.get("/ai-analysis/findings").json()["items"][0]
    finding_id = finding["id"]
    assert finding["explanation"]
    assert "storage_location" not in finding
    acknowledged = client.patch(
        f"/ai-analysis/findings/{finding_id}", json={"status": "acknowledged"}
    )
    assert acknowledged.status_code == 200
    assert acknowledged.json()["reviewed_by_id"] == str(owner.id)
    assert client.patch(f"/ai-analysis/findings/{finding_id}", json={"status": "resolved"}).json()[
        "resolved_at"
    ]
    assert (
        client.patch(f"/ai-analysis/findings/{finding_id}", json={"status": "open"}).json()[
            "resolved_at"
        ]
        is None
    )
    current["user"] = viewer
    assert (
        client.patch(f"/ai-analysis/findings/{finding_id}", json={"status": "resolved"}).status_code
        == 403
    )


def test_cross_user_findings_and_document_analysis_are_isolated(phase9a_client) -> None:
    client, current, _, other, _, first, private = phase9a_client
    assert client.get(f"/documents/{private.id}/ai-analysis").status_code == 404
    owner_ids = {
        item["document_id"] for item in client.get("/ai-analysis/findings").json()["items"]
    }
    assert str(first.id) in owner_ids
    assert str(private.id) not in owner_ids
    current["user"] = other
    other_ids = {
        item["document_id"] for item in client.get("/ai-analysis/findings").json()["items"]
    }
    assert str(private.id) in other_ids
    assert str(first.id) not in other_ids


def test_analytics_kpis_multi_currency_trends_and_filters(phase9a_client) -> None:
    client, _, _, _, _, _, _ = phase9a_client
    payload = client.get("/analytics/summary", params={"range": "all"}).json()
    assert payload["kpis"]["total_documents"] == 2
    assert payload["kpis"]["total_invoices"] == 2
    totals = {item["currency"]: item["total"] for item in payload["kpis"]["currency_totals"]}
    assert totals == {"EUR": "50.00", "USD": "110.00"}
    assert payload["trends"]["invoice_count"]
    assert payload["trends"]["top_vendors"]
    filtered = client.get("/analytics/summary", params={"range": "all", "vendor": "Acme"}).json()
    assert filtered["kpis"]["total_documents"] == 1
    assert (
        client.get(
            "/analytics/summary",
            params={"range": "custom", "start_date": "2026-09-03", "end_date": "2026-01-01"},
        ).status_code
        == 422
    )


@pytest.mark.parametrize("report_type", ["financial", "ai-analysis", "processing-quality"])
def test_report_preview_uses_real_owned_data(phase9a_client, report_type: str) -> None:
    client, _, _, _, _, _, _ = phase9a_client
    response = client.get(f"/reports/{report_type}", params={"range": "all"})
    assert response.status_code == 200
    assert response.json()["report_type"] == report_type
    if report_type == "ai-analysis":
        assert response.json()["findings_by_category"]
        assert response.json()["review_status"]
    assert "storage_location" not in response.text


@pytest.mark.parametrize(
    ("export_format", "content_type"),
    [
        ("csv", "text/csv"),
        ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("json", "application/json"),
    ],
)
def test_report_exports_are_safe_and_filtered(
    phase9a_client, export_format: str, content_type: str
) -> None:
    client, _, _, _, _, _, _ = phase9a_client
    response = client.get(
        f"/reports/financial/exports/{export_format}",
        params={"range": "all", "vendor": "Acme"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["content-disposition"].startswith(
        'attachment; filename="invoxa-financial-report-'
    )
    assert b"Private Vendor" not in response.content


def test_report_and_analytics_require_valid_inputs_and_owned_data(phase9a_client) -> None:
    client, current, _, other, _, _, _ = phase9a_client
    assert client.get("/reports/not-a-report").status_code == 422
    assert client.get("/analytics/summary", params={"range": "invalid"}).status_code == 422
    current["user"] = other
    report = client.get("/reports/financial", params={"range": "all"})
    assert report.status_code == 200
    assert "Acme" not in json.dumps(report.json())
