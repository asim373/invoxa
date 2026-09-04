import csv
import io
import json
import uuid
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from openpyxl import Workbook
from pydantic import BaseModel, ConfigDict
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session, load_only, selectinload

from apps.api.app.ai_analysis import analyze_document
from apps.api.app.auth import get_current_editor, get_current_user
from apps.api.app.database import get_db
from apps.api.app.export_service import sanitize_cell_text
from apps.api.app.models import (
    AIAnalysisStatus,
    AIFinding,
    AIFindingCategory,
    AIFindingSeverity,
    AIFindingStatus,
    Document,
    DocumentStatus,
    InvoiceExtraction,
    User,
)

router = APIRouter(tags=["AI Analysis", "analytics", "reports"])
ReportType = Literal["financial", "ai-analysis", "processing-quality"]
ExportFormat = Literal["csv", "xlsx", "json"]
RangeName = Literal["7d", "30d", "90d", "year", "custom", "all"]


class FindingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    category: AIFindingCategory
    severity: AIFindingSeverity
    status: AIFindingStatus
    title: str
    explanation: str
    evidence: dict[str, object]
    affected_fields: list[str]
    confidence: Decimal | None
    observed_value: str | None
    expected_value: str | None
    reviewed_by_id: uuid.UUID | None
    reviewed_at: datetime | None
    resolved_at: datetime | None
    created_at: datetime
    updated_at: datetime


class FindingListItem(FindingResponse):
    document_filename: str
    invoice_number: str | None
    vendor_name: str | None


class PaginatedFindings(BaseModel):
    items: list[FindingListItem]
    total: int
    page: int
    page_size: int
    total_pages: int


class ReviewRequest(BaseModel):
    status: AIFindingStatus


class DocumentAnalysisSummary(BaseModel):
    document_id: uuid.UUID
    analysis_status: AIAnalysisStatus
    analysis_error: str | None
    analyzed_at: datetime | None
    risk_level: Literal["clear", "review", "high"]
    summary: str
    counts: dict[str, int]
    findings: list[FindingResponse]


class AnalyticsFilters(BaseModel):
    start_date: date | None = None
    end_date: date | None = None
    vendor: str | None = None
    processing_status: DocumentStatus | None = None
    validation_status: Literal["valid", "invalid", "missing"] | None = None
    ai_severity: AIFindingSeverity | None = None
    ai_status: AIFindingStatus | None = None
    mime_type: str | None = None


def _date_bounds(
    range_name: RangeName, start_date: date | None, end_date: date | None
) -> tuple[date | None, date | None]:
    today = datetime.now(UTC).date()
    if range_name == "7d":
        return today - timedelta(days=6), today
    if range_name == "30d":
        return today - timedelta(days=29), today
    if range_name == "90d":
        return today - timedelta(days=89), today
    if range_name == "year":
        return date(today.year, 1, 1), today
    if range_name == "all":
        return None, None
    if start_date is None or end_date is None:
        raise HTTPException(
            status_code=422, detail="Custom range requires start_date and end_date."
        )
    if start_date > end_date:
        raise HTTPException(status_code=422, detail="start_date must not be after end_date.")
    if (end_date - start_date).days > 3660:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 10 years.")
    return start_date, end_date


def _filters(
    range_name: RangeName,
    start_date: date | None,
    end_date: date | None,
    vendor: str | None,
    processing_status: DocumentStatus | None,
    validation_status: Literal["valid", "invalid", "missing"] | None,
    ai_severity: AIFindingSeverity | None,
    ai_status: AIFindingStatus | None,
    mime_type: str | None,
) -> AnalyticsFilters:
    start, end = _date_bounds(range_name, start_date, end_date)
    return AnalyticsFilters(
        start_date=start,
        end_date=end,
        vendor=vendor.strip()[:255] if vendor and vendor.strip() else None,
        processing_status=processing_status,
        validation_status=validation_status,
        ai_severity=ai_severity,
        ai_status=ai_status,
        mime_type=mime_type.strip()[:255] if mime_type and mime_type.strip() else None,
    )


def _document_statement(owner_id: uuid.UUID, filters: AnalyticsFilters):
    statement = (
        select(Document)
        .where(Document.owner_id == owner_id, Document.owner_id.is_not(None))
        .options(
            load_only(
                Document.id,
                Document.owner_id,
                Document.original_filename,
                Document.mime_type,
                Document.status,
                Document.created_at,
            ),
            selectinload(Document.invoice_extraction),
            selectinload(Document.ai_findings),
        )
    )
    if filters.start_date:
        statement = statement.where(
            Document.created_at
            >= datetime.combine(filters.start_date, datetime.min.time(), tzinfo=UTC)
        )
    if filters.end_date:
        statement = statement.where(
            Document.created_at
            < datetime.combine(
                filters.end_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC
            )
        )
    if filters.processing_status:
        statement = statement.where(Document.status == filters.processing_status)
    if filters.mime_type:
        statement = statement.where(Document.mime_type == filters.mime_type)
    if filters.vendor:
        statement = statement.where(
            Document.invoice_extraction.has(
                func.lower(InvoiceExtraction.vendor_name) == filters.vendor.casefold()
            )
        )
    if filters.validation_status == "valid":
        statement = statement.where(
            Document.invoice_extraction.has(InvoiceExtraction.is_valid.is_(True))
        )
    elif filters.validation_status == "invalid":
        statement = statement.where(
            Document.invoice_extraction.has(InvoiceExtraction.is_valid.is_(False))
        )
    elif filters.validation_status == "missing":
        statement = statement.where(~Document.invoice_extraction.has())
    finding_criteria = []
    if filters.ai_severity:
        finding_criteria.append(AIFinding.severity == filters.ai_severity)
    if filters.ai_status:
        finding_criteria.append(AIFinding.status == filters.ai_status)
    if finding_criteria:
        statement = statement.where(Document.ai_findings.any(and_(*finding_criteria)))
    return statement


def _risk_summary(
    findings: list[AIFinding],
) -> tuple[Literal["clear", "review", "high"], str, dict[str, int]]:
    unresolved = [finding for finding in findings if finding.status != AIFindingStatus.RESOLVED]
    counts = Counter(finding.severity.value for finding in unresolved)
    if counts[AIFindingSeverity.CRITICAL.value] or counts[AIFindingSeverity.HIGH.value]:
        return "high", "High-risk findings detected", dict(counts)
    if unresolved:
        return "review", "Review recommended", dict(counts)
    return "clear", "No significant issues found", dict(counts)


def _finding_item(finding: AIFinding, document: Document) -> FindingListItem:
    extraction = document.invoice_extraction
    return FindingListItem(
        **FindingResponse.model_validate(finding).model_dump(),
        document_filename=document.original_filename,
        invoice_number=extraction.invoice_number if extraction else None,
        vendor_name=extraction.vendor_name if extraction else None,
    )


@router.get("/ai-analysis/findings", response_model=PaginatedFindings)
def list_findings(
    severity: AIFindingSeverity | None = None,
    finding_status: AIFindingStatus | None = Query(default=None, alias="status"),  # noqa: B008
    category: AIFindingCategory | None = None,
    document_id: uuid.UUID | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=100),
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> PaginatedFindings:
    clauses = [Document.owner_id == current_user.id, Document.owner_id.is_not(None)]
    if severity:
        clauses.append(AIFinding.severity == severity)
    if finding_status:
        clauses.append(AIFinding.status == finding_status)
    if category:
        clauses.append(AIFinding.category == category)
    if document_id:
        clauses.append(AIFinding.document_id == document_id)
    total = database.scalar(select(func.count(AIFinding.id)).join(Document).where(*clauses)) or 0
    rows = database.execute(
        select(AIFinding, Document)
        .join(Document)
        .where(*clauses)
        .options(selectinload(Document.invoice_extraction))
        .order_by(AIFinding.created_at.desc(), AIFinding.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return PaginatedFindings(
        items=[_finding_item(finding, document) for finding, document in rows],
        total=total,
        page=page,
        page_size=page_size,
        total_pages=(total + page_size - 1) // page_size,
    )


@router.get("/documents/{document_id}/ai-analysis", response_model=DocumentAnalysisSummary)
def document_analysis(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> DocumentAnalysisSummary:
    document = database.scalar(
        select(Document)
        .where(Document.id == document_id, Document.owner_id == current_user.id)
        .options(selectinload(Document.ai_findings))
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    risk, summary, counts = _risk_summary(document.ai_findings)
    return DocumentAnalysisSummary(
        document_id=document.id,
        analysis_status=document.ai_analysis_status,
        analysis_error=document.ai_analysis_error,
        analyzed_at=document.ai_analyzed_at,
        risk_level=risk,
        summary=summary,
        counts=counts,
        findings=[FindingResponse.model_validate(finding) for finding in document.ai_findings],
    )


@router.post("/documents/{document_id}/ai-analysis", response_model=DocumentAnalysisSummary)
def reanalyze_document(
    document_id: uuid.UUID,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_editor),  # noqa: B008
) -> DocumentAnalysisSummary:
    document = database.scalar(
        select(Document)
        .where(Document.id == document_id, Document.owner_id == current_user.id)
        .options(
            selectinload(Document.invoice_extraction).selectinload(InvoiceExtraction.line_items),
            selectinload(Document.ai_findings),
        )
    )
    if document is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    if document.status != DocumentStatus.COMPLETED:
        raise HTTPException(status_code=409, detail="AI Analysis requires a completed document.")
    try:
        analyze_document(database, document)
        database.commit()
        database.refresh(document)
    except Exception:
        database.rollback()
        document = database.get(Document, document_id)
        if document is not None:
            document.ai_analysis_status = AIAnalysisStatus.FAILED
            document.ai_analysis_error = (
                "AI Analysis could not be completed. The extracted document remains available."
            )
            database.commit()
        raise HTTPException(status_code=503, detail="AI Analysis could not be completed.") from None
    return document_analysis(document_id, database, current_user)


@router.patch("/ai-analysis/findings/{finding_id}", response_model=FindingResponse)
def review_finding(
    finding_id: uuid.UUID,
    payload: ReviewRequest,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_editor),  # noqa: B008
) -> AIFinding:
    finding = database.scalar(
        select(AIFinding)
        .join(Document)
        .where(AIFinding.id == finding_id, Document.owner_id == current_user.id)
    )
    if finding is None:
        raise HTTPException(status_code=404, detail="AI finding not found.")
    now = datetime.now(UTC)
    finding.status = payload.status
    finding.reviewed_by_id = current_user.id
    finding.reviewed_at = now
    finding.resolved_at = now if payload.status == AIFindingStatus.RESOLVED else None
    database.commit()
    database.refresh(finding)
    return finding


def _analytics_payload(documents: Sequence[Document], filters: AnalyticsFilters) -> dict[str, Any]:
    invoices = [
        document.invoice_extraction
        for document in documents
        if document.invoice_extraction is not None
    ]
    findings = [finding for document in documents for finding in document.ai_findings]
    totals: dict[str, Decimal] = defaultdict(Decimal)
    taxes: dict[str, Decimal] = defaultdict(Decimal)
    counts: dict[str, int] = defaultdict(int)
    vendor_totals: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    spend_time: dict[tuple[str, str], Decimal] = defaultdict(Decimal)
    invoice_time: Counter[str] = Counter()
    for document in documents:
        extraction = document.invoice_extraction
        if extraction is None:
            continue
        period = (extraction.invoice_date or document.created_at.date()).isoformat()[:7]
        invoice_time[period] += 1
        if extraction.total is not None and extraction.currency:
            totals[extraction.currency] += extraction.total
            counts[extraction.currency] += 1
            spend_time[(period, extraction.currency)] += extraction.total
            if extraction.vendor_name:
                vendor_totals[(extraction.vendor_name, extraction.currency)] += extraction.total
        if extraction.tax is not None and extraction.currency:
            taxes[extraction.currency] += extraction.tax
    unresolved = [finding for finding in findings if finding.status != AIFindingStatus.RESOLVED]
    needs_review_ids = {finding.document_id for finding in unresolved}
    needs_review_ids.update(
        document.id
        for document in documents
        if document.invoice_extraction and not document.invoice_extraction.is_valid
    )
    return {
        "filters": filters.model_dump(mode="json"),
        "kpis": {
            "total_documents": len(documents),
            "completed_documents": sum(
                document.status == DocumentStatus.COMPLETED for document in documents
            ),
            "total_invoices": len(invoices),
            "currency_totals": [
                {
                    "currency": currency,
                    "total": str(total),
                    "tax": str(taxes[currency]),
                    "average": str(total / counts[currency]),
                }
                for currency, total in sorted(totals.items())
            ],
            "needs_review": len(needs_review_ids),
            "ai_findings": len(findings),
            "unresolved_ai_findings": len(unresolved),
            "high_severity_ai_findings": sum(
                finding.severity in {AIFindingSeverity.HIGH, AIFindingSeverity.CRITICAL}
                for finding in unresolved
            ),
            "validation_issue_count": sum(not invoice.is_valid for invoice in invoices),
        },
        "trends": {
            "invoice_spend": [
                {"period": period, "currency": currency, "total": str(total)}
                for (period, currency), total in sorted(spend_time.items())
            ],
            "invoice_count": [
                {"period": period, "count": count} for period, count in sorted(invoice_time.items())
            ],
            "top_vendors": [
                {"vendor": vendor, "currency": currency, "total": str(total)}
                for (vendor, currency), total in sorted(
                    vendor_totals.items(), key=lambda item: item[1], reverse=True
                )[:10]
            ],
            "document_status": dict(Counter(document.status.value for document in documents)),
            "validation_status": dict(
                Counter("valid" if invoice.is_valid else "needs_review" for invoice in invoices)
            ),
            "findings_by_severity": dict(Counter(finding.severity.value for finding in findings)),
            "findings_over_time": dict(
                Counter(finding.created_at.date().isoformat() for finding in findings)
            ),
        },
    }


@router.get("/analytics/summary")
def analytics_summary(
    range: RangeName = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
    vendor: str | None = None,
    processing_status: DocumentStatus | None = None,
    validation_status: Literal["valid", "invalid", "missing"] | None = None,
    ai_severity: AIFindingSeverity | None = None,
    ai_status: AIFindingStatus | None = None,
    mime_type: str | None = None,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, object]:
    selected = _filters(
        range,
        start_date,
        end_date,
        vendor,
        processing_status,
        validation_status,
        ai_severity,
        ai_status,
        mime_type,
    )
    documents = database.scalars(_document_statement(current_user.id, selected)).unique().all()
    return _analytics_payload(documents, selected)


def _report_payload(
    report_type: ReportType, analytics: dict[str, Any], documents: Sequence[Document]
) -> dict[str, object]:
    if not documents:
        raise HTTPException(
            status_code=404, detail="No data is available for the selected report filters."
        )
    kpis = analytics["kpis"]
    trends = analytics["trends"]
    if report_type == "financial":
        return {
            "report_type": report_type,
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": kpis,
            "top_vendors": trends["top_vendors"],
            "spend_over_time": trends["invoice_spend"],
        }
    if report_type == "ai-analysis":
        report_findings = [finding for document in documents for finding in document.ai_findings]
        affected = [
            {
                "document_id": str(document.id),
                "filename": document.original_filename,
                "findings": len(document.ai_findings),
                "unresolved": sum(
                    f.status != AIFindingStatus.RESOLVED for f in document.ai_findings
                ),
            }
            for document in documents
            if document.ai_findings
        ]
        return {
            "report_type": report_type,
            "generated_at": datetime.now(UTC).isoformat(),
            "summary": kpis,
            "findings_by_severity": trends["findings_by_severity"],
            "findings_by_category": dict(
                Counter(finding.category.value for finding in report_findings)
            ),
            "review_status": dict(Counter(finding.status.value for finding in report_findings)),
            "affected_documents": affected,
        }
    low_confidence = sum(
        any(f.category == AIFindingCategory.CONFIDENCE for f in d.ai_findings) for d in documents
    )
    return {
        "report_type": report_type,
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": kpis,
        "document_status": trends["document_status"],
        "validation_status": trends["validation_status"],
        "low_confidence_documents": low_confidence,
    }


def _make_report(
    database: Session, owner_id: uuid.UUID, report_type: ReportType, selected: AnalyticsFilters
) -> dict[str, object]:
    documents = database.scalars(_document_statement(owner_id, selected)).unique().all()
    analytics = _analytics_payload(documents, selected)
    report = _report_payload(report_type, analytics, documents)
    report["filters"] = selected.model_dump(mode="json")
    return report


@router.get("/reports/{report_type}")
def report_preview(
    report_type: ReportType,
    range: RangeName = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
    vendor: str | None = None,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> dict[str, object]:
    selected = _filters(range, start_date, end_date, vendor, None, None, None, None, None)
    return _make_report(database, current_user.id, report_type, selected)


def _flat_rows(report: dict[str, object]) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for key, value in report.items():
        if isinstance(value, (dict, list)):
            rows.append((key, json.dumps(value, default=str, separators=(",", ":"))))
        else:
            rows.append((key, str(value)))
    return rows


@router.get("/reports/{report_type}/exports/{export_format}")
def export_report(
    report_type: ReportType,
    export_format: ExportFormat,
    range: RangeName = "30d",
    start_date: date | None = None,
    end_date: date | None = None,
    vendor: str | None = None,
    database: Session = Depends(get_db),  # noqa: B008
    current_user: User = Depends(get_current_user),  # noqa: B008
) -> Response:
    selected = _filters(range, start_date, end_date, vendor, None, None, None, None, None)
    report = _make_report(database, current_user.id, report_type, selected)
    filename = f"invoxa-{report_type}-report-{datetime.now(UTC):%Y%m%d}.{export_format}"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if export_format == "json":
        return Response(
            json.dumps(report, default=str, separators=(",", ":")),
            media_type="application/json",
            headers=headers,
        )
    rows = _flat_rows(report)
    if export_format == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["metric", "value"])
        writer.writerows(
            (sanitize_cell_text(key), sanitize_cell_text(value)) for key, value in rows
        )
        return Response(stream.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Report"
    sheet.append(["Metric", "Value"])
    for key, value in rows:
        sheet.append([sanitize_cell_text(key), sanitize_cell_text(value)])
    sheet.freeze_panes = "A2"
    sheet.column_dimensions["A"].width = 28
    sheet.column_dimensions["B"].width = 90
    binary = io.BytesIO()
    workbook.save(binary)
    workbook.close()
    return Response(
        binary.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
