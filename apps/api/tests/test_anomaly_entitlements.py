"""Plan-specific analysis generation and persisted-finding disclosure boundaries."""

import io
import uuid
from datetime import UTC, datetime, timedelta
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.ai_analysis import generate_finding_specs
from apps.api.app.auth import get_current_user
from apps.api.app.database import Base, get_db
from apps.api.app.main import app
from apps.api.app.models import (
    AIFinding,
    AIFindingCategory,
    AIFindingSeverity,
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
)
from apps.api.app.tenant import TenantContext, get_current_tenant
from apps.api.tests.test_phase9a import add_invoice, add_line, add_user


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def seed(database, plan):
    organization = Organization(name="Disposable", slug=uuid.uuid4().hex, plan_code=plan)
    database.add(organization)
    database.flush()
    owner = add_user(database, f"{uuid.uuid4().hex}@example.com")
    return organization, owner


@pytest.mark.parametrize("plan", ["standard", "professional", "enterprise"])
def test_basic_financial_checks_remain_available(database, plan):
    organization, owner = seed(database, plan)
    invoice = add_invoice(
        database,
        owner,
        organization=organization,
        number=None,
        vendor=None,
        invoice_date=datetime.now(UTC).date() + timedelta(days=5),
        subtotal="100.00",
        tax="-5.00",
        total="150.00",
        valid=False,
    )
    add_line(invoice, "2", "25", "70")
    categories = {finding.category for finding in generate_finding_specs(database, invoice)}
    assert {
        AIFindingCategory.ARITHMETIC,
        AIFindingCategory.MISSING,
        AIFindingCategory.TAX,
        AIFindingCategory.DATE,
        AIFindingCategory.CONFIDENCE,
    } <= categories


@pytest.mark.parametrize("plan", ["standard", "professional", "enterprise"])
def test_history_and_probable_duplicates_require_advanced(database, plan):
    organization, owner = seed(database, plan)
    originals = [
        add_invoice(
            database, owner, organization=organization, number=f"BASE-{index}", total="100.00"
        )
        for index in range(5)
    ]
    outlier = add_invoice(
        database, owner, organization=organization, number="OUTLIER", total="1000.00"
    )
    categories = {finding.category for finding in generate_finding_specs(database, outlier)}
    assert (AIFindingCategory.AMOUNT in categories) == (plan != "standard")
    assert (AIFindingCategory.VENDOR in categories) == (plan != "standard")
    probable = add_invoice(
        database, owner, organization=organization, number="BASE-0", total="150.00"
    )
    titles = {finding.title for finding in generate_finding_specs(database, probable)}
    assert ("Possible duplicate invoice" in titles) == (plan != "standard")
    exact_file = add_invoice(
        database, owner, organization=organization, checksum=originals[0].checksum
    )
    assert "Exact duplicate document detected" in {
        f.title for f in generate_finding_specs(database, exact_file)
    }
    exact_fields = add_invoice(
        database, owner, organization=organization, number="BASE-0", total="100.00"
    )
    assert "Exact duplicate invoice detected" in {
        f.title for f in generate_finding_specs(database, exact_fields)
    }


@pytest.mark.parametrize("plan", ["standard", "professional", "enterprise"])
def test_persisted_findings_filtered_across_all_response_paths(database, plan):
    organization, owner = seed(database, "professional")
    document = add_invoice(database, owner, organization=organization)
    records = []
    for category, title in [
        (AIFindingCategory.AMOUNT, "HIDDEN_AMOUNT"),
        (AIFindingCategory.VENDOR, "HIDDEN_VENDOR"),
        (AIFindingCategory.DUPLICATE, "Possible duplicate invoice"),
        (AIFindingCategory.DUPLICATE, "Exact duplicate invoice detected"),
        (AIFindingCategory.MISSING, "Basic finding"),
    ]:
        finding = AIFinding(
            document_id=document.id,
            category=category,
            title=title,
            severity=AIFindingSeverity.MEDIUM,
            signature=uuid.uuid4().hex,
            explanation=title,
        )
        database.add(finding)
        records.append(finding)
    other, other_owner = seed(database, "professional")
    other_document = add_invoice(database, other_owner, organization=other)
    database.add(
        AIFinding(
            document_id=other_document.id,
            category=AIFindingCategory.MISSING,
            title="OTHER_TENANT",
            explanation="OTHER_TENANT",
            severity=AIFindingSeverity.HIGH,
            signature=uuid.uuid4().hex,
        )
    )
    database.commit()
    # Simulate retained history following a downgrade, including a warm identity map.
    assert len(document.ai_findings) == 5
    organization.plan_code = plan
    database.commit()
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_current_user] = lambda: owner
    membership = OrganizationMember(
        organization=organization,
        user=owner,
        role=OrganizationMemberRole.OWNER,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add(membership)
    database.commit()
    app.dependency_overrides[get_current_tenant] = lambda: TenantContext(organization, membership)
    with TestClient(app) as client:
        expected = 2 if plan == "standard" else 5
        listed = client.get("/ai-analysis/findings").json()
        assert listed["total"] == expected
        assert len(listed["items"]) == expected
        summary = client.get(f"/documents/{document.id}/ai-analysis").json()
        assert len(summary["findings"]) == expected
        assert client.get(f"/documents/{other_document.id}/ai-analysis").status_code == 404
        filtered = client.get("/ai-analysis/findings?category=unusual_amount").json()
        assert filtered["total"] == (0 if plan == "standard" else 1)
        analytics = client.get("/analytics/summary?range=all").json()
        assert analytics["kpis"]["ai_findings"] == expected
        report = client.get("/reports/ai-analysis?range=all").json()
        assert report["summary"]["ai_findings"] == expected
        assert ("unusual_amount" in report["findings_by_category"]) == (plan != "standard")
        assert ("vendor_pattern" in report["findings_by_category"]) == (plan != "standard")
        spreadsheet = client.get("/reports/ai-analysis/exports/xlsx?range=all")
        assert spreadsheet.status_code == 200
        with ZipFile(io.BytesIO(spreadsheet.content)) as archive:
            xml = b"".join(
                archive.read(name) for name in archive.namelist() if name.endswith(".xml")
            )
        assert b"OTHER_TENANT" not in xml
        if plan == "standard":
            assert b"unusual_amount" not in xml and b"vendor_pattern" not in xml
        for route in [
            "/analytics/summary?range=all",
            "/reports/ai-analysis?range=all",
            "/reports/financial?range=all",
            "/reports/processing-quality?range=all",
            "/reports/ai-analysis/exports/json?range=all",
            "/reports/ai-analysis/exports/csv?range=all",
        ]:
            response = client.get(route)
            assert response.status_code == 200, (route, response.status_code)
            assert "OTHER_TENANT" not in response.text
            if plan == "standard":
                assert "HIDDEN_AMOUNT" not in response.text
                assert "HIDDEN_VENDOR" not in response.text
        result = client.patch(
            f"/ai-analysis/findings/{records[0].id}", json={"status": "acknowledged"}
        )
        assert result.status_code == (404 if plan == "standard" else 200)
