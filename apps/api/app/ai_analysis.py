import hashlib
import logging
import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from apps.api.app.models import (
    AIAnalysisStatus,
    AIFinding,
    AIFindingCategory,
    AIFindingSeverity,
    Document,
    DocumentStatus,
    InvoiceExtraction,
)

logger = logging.getLogger(__name__)
MONEY_TOLERANCE = Decimal("0.02")
MIN_GLOBAL_SAMPLE = 5
MIN_VENDOR_SAMPLE = 4


@dataclass(frozen=True)
class FindingSpec:
    key: str
    category: AIFindingCategory
    severity: AIFindingSeverity
    title: str
    explanation: str
    evidence: dict[str, object] = field(default_factory=dict)
    affected_fields: list[str] = field(default_factory=list)
    confidence: Decimal | None = None
    observed_value: str | None = None
    expected_value: str | None = None

    @property
    def signature(self) -> str:
        raw = f"{self.category.value}:{self.key}".encode()
        return hashlib.sha256(raw).hexdigest()


def _money(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:.2f}"


def _normalized(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def _missing_findings(extraction: InvoiceExtraction) -> list[FindingSpec]:
    labels = {
        "invoice_number": (extraction.invoice_number, "invoice number"),
        "vendor_name": (extraction.vendor_name, "vendor"),
        "invoice_date": (extraction.invoice_date, "invoice date"),
        "total": (extraction.total, "total"),
        "currency": (extraction.currency, "currency"),
    }
    missing = [field_name for field_name, (value, _) in labels.items() if value in (None, "")]
    if not missing:
        return []
    readable = ", ".join(labels[name][1] for name in missing)
    severity = AIFindingSeverity.HIGH if "total" in missing else AIFindingSeverity.MEDIUM
    return [
        FindingSpec(
            key="critical-fields",
            category=AIFindingCategory.MISSING,
            severity=severity,
            title="Critical invoice information is missing",
            explanation=f"Review recommended because extraction did not identify: {readable}.",
            evidence={"missing_fields": missing},
            affected_fields=missing,
        )
    ]


def _arithmetic_findings(extraction: InvoiceExtraction) -> list[FindingSpec]:
    findings: list[FindingSpec] = []
    if all(value is not None for value in (extraction.subtotal, extraction.tax, extraction.total)):
        assert extraction.subtotal is not None
        assert extraction.tax is not None
        assert extraction.total is not None
        expected = extraction.subtotal + extraction.tax
        if abs(expected - extraction.total) > MONEY_TOLERANCE:
            findings.append(
                FindingSpec(
                    key="subtotal-tax-total",
                    category=AIFindingCategory.ARITHMETIC,
                    severity=AIFindingSeverity.HIGH,
                    title="Invoice totals do not reconcile",
                    explanation=(
                        f"Invoice total is {_money(extraction.total)}, but subtotal plus tax "
                        f"equals {_money(expected)}."
                    ),
                    evidence={
                        "subtotal": _money(extraction.subtotal),
                        "tax": _money(extraction.tax),
                        "calculated_total": _money(expected),
                    },
                    affected_fields=["subtotal", "tax", "total"],
                    observed_value=_money(extraction.total),
                    expected_value=_money(expected),
                )
            )
    for item in extraction.line_items:
        expected = item.quantity * item.unit_price
        if abs(expected - item.line_total) > MONEY_TOLERANCE:
            findings.append(
                FindingSpec(
                    key=f"line-item-{item.position}",
                    category=AIFindingCategory.ARITHMETIC,
                    severity=AIFindingSeverity.MEDIUM,
                    title="Line-item amount is inconsistent",
                    explanation=(
                        f"Line {item.position + 1} total is {_money(item.line_total)}, "
                        "but quantity "
                        f"multiplied by unit price equals {_money(expected)}."
                    ),
                    evidence={"line_item_position": item.position, "description": item.description},
                    affected_fields=[
                        "line_items.quantity",
                        "line_items.unit_price",
                        "line_items.line_total",
                    ],
                    observed_value=_money(item.line_total),
                    expected_value=_money(expected),
                )
            )
    if extraction.line_items and extraction.subtotal is not None:
        line_sum = sum((item.line_total for item in extraction.line_items), Decimal("0"))
        if abs(line_sum - extraction.subtotal) > MONEY_TOLERANCE:
            findings.append(
                FindingSpec(
                    key="line-items-subtotal",
                    category=AIFindingCategory.ARITHMETIC,
                    severity=AIFindingSeverity.HIGH,
                    title="Line items do not match the subtotal",
                    explanation=(
                        f"Line-item totals add up to {_money(line_sum)}, while the extracted "
                        f"subtotal is {_money(extraction.subtotal)}."
                    ),
                    evidence={"line_item_count": len(extraction.line_items)},
                    affected_fields=["line_items", "subtotal"],
                    observed_value=_money(line_sum),
                    expected_value=_money(extraction.subtotal),
                )
            )
    return findings


def _tax_findings(extraction: InvoiceExtraction) -> list[FindingSpec]:
    if extraction.tax is None or extraction.tax >= 0:
        return []
    return [
        FindingSpec(
            key="negative-tax",
            category=AIFindingCategory.TAX,
            severity=AIFindingSeverity.MEDIUM,
            title="Tax amount is negative",
            explanation=(
                f"The extracted tax is {_money(extraction.tax)}. "
                "Verify that the value and sign are correct."
            ),
            evidence={"tax": _money(extraction.tax)},
            affected_fields=["tax"],
            observed_value=_money(extraction.tax),
            expected_value="zero or positive amount",
        )
    ]


def _date_findings(document: Document, extraction: InvoiceExtraction) -> list[FindingSpec]:
    if extraction.invoice_date is None:
        return []
    upload_date = document.created_at.date()
    if extraction.invoice_date > upload_date + timedelta(days=1):
        return [
            FindingSpec(
                key="future-date",
                category=AIFindingCategory.DATE,
                severity=AIFindingSeverity.HIGH,
                title="Invoice date is after the upload date",
                explanation=(
                    f"The invoice date {extraction.invoice_date.isoformat()} is later than the "
                    f"upload date {upload_date.isoformat()}; review the extracted date."
                ),
                evidence={
                    "invoice_date": extraction.invoice_date.isoformat(),
                    "upload_date": upload_date.isoformat(),
                },
                affected_fields=["invoice_date"],
            )
        ]
    if extraction.invoice_date < upload_date - timedelta(days=365):
        age = (upload_date - extraction.invoice_date).days
        return [
            FindingSpec(
                key="old-date",
                category=AIFindingCategory.DATE,
                severity=AIFindingSeverity.LOW,
                title="Invoice date is unusually old",
                explanation=(
                    f"The invoice predates upload by {age} days. "
                    "Confirm that this historical document is expected."
                ),
                evidence={"age_days": age},
                affected_fields=["invoice_date"],
            )
        ]
    return []


def _duplicate_findings(
    database: Session, document: Document, extraction: InvoiceExtraction
) -> list[FindingSpec]:
    candidates = database.scalars(
        select(Document)
        .where(
            Document.owner_id == document.owner_id,
            Document.id != document.id,
            Document.status == DocumentStatus.COMPLETED,
        )
        .options(selectinload(Document.invoice_extraction))
    ).all()
    current_vendor = _normalized(extraction.vendor_name)
    for candidate in candidates:
        other = candidate.invoice_extraction
        if candidate.checksum == document.checksum:
            return [
                FindingSpec(
                    key=f"exact-checksum-{candidate.id}",
                    category=AIFindingCategory.DUPLICATE,
                    severity=AIFindingSeverity.HIGH,
                    title="Exact duplicate document detected",
                    explanation=(
                        "This file has the same document fingerprint as "
                        f"{candidate.original_filename}."
                    ),
                    evidence={
                        "related_document_id": str(candidate.id),
                        "signals": ["document fingerprint"],
                    },
                    confidence=Decimal("1.0000"),
                )
            ]
        if other is None:
            continue
        signals: list[str] = []
        if extraction.invoice_number and _normalized(extraction.invoice_number) == _normalized(
            other.invoice_number
        ):
            signals.append("invoice number")
        if current_vendor and current_vendor == _normalized(other.vendor_name):
            signals.append("vendor")
        if extraction.invoice_date and extraction.invoice_date == other.invoice_date:
            signals.append("invoice date")
        if (
            extraction.total is not None
            and extraction.total == other.total
            and extraction.currency == other.currency
        ):
            signals.append("currency and total")
        if len(signals) == 4:
            return [
                FindingSpec(
                    key=f"exact-fields-{candidate.id}",
                    category=AIFindingCategory.DUPLICATE,
                    severity=AIFindingSeverity.HIGH,
                    title="Exact duplicate invoice detected",
                    explanation=(
                        f"This invoice matches {candidate.original_filename} on invoice number, "
                        "vendor, date, currency and total."
                    ),
                    evidence={"related_document_id": str(candidate.id), "signals": signals},
                    confidence=Decimal("0.9800"),
                )
            ]
        if len(signals) >= 3 and ("invoice number" in signals or "currency and total" in signals):
            confidence = Decimal("0.8000") if len(signals) == 3 else Decimal("0.9000")
            return [
                FindingSpec(
                    key=f"probable-{candidate.id}",
                    category=AIFindingCategory.DUPLICATE,
                    severity=AIFindingSeverity.MEDIUM,
                    title="Possible duplicate invoice",
                    explanation=(
                        f"This invoice shares {', '.join(signals)} with "
                        f"{candidate.original_filename}; review both documents."
                    ),
                    evidence={"related_document_id": str(candidate.id), "signals": signals},
                    confidence=confidence,
                )
            ]
    return []


def _robust_anomaly(values: list[Decimal], current: Decimal) -> tuple[bool, Decimal, Decimal]:
    median = Decimal(str(statistics.median(values)))
    deviations = [abs(value - median) for value in values]
    mad = Decimal(str(statistics.median(deviations)))
    if mad == 0:
        threshold = max(abs(median) * Decimal("0.50"), Decimal("1.00"))
        distance = abs(current - median)
        return distance > threshold, median, distance / threshold if threshold else Decimal("0")
    robust_z = Decimal("0.6745") * abs(current - median) / mad
    return robust_z > Decimal("3.5"), median, robust_z / Decimal("3.5")


def _amount_findings(
    database: Session, document: Document, extraction: InvoiceExtraction
) -> list[FindingSpec]:
    if extraction.total is None or not extraction.currency:
        return []
    rows = database.execute(
        select(InvoiceExtraction.total, InvoiceExtraction.vendor_name)
        .join(Document, Document.id == InvoiceExtraction.document_id)
        .where(
            Document.owner_id == document.owner_id,
            Document.id != document.id,
            Document.status == DocumentStatus.COMPLETED,
            InvoiceExtraction.currency == extraction.currency,
            InvoiceExtraction.total.is_not(None),
        )
    ).all()
    findings: list[FindingSpec] = []
    totals = [row.total for row in rows if row.total is not None]
    if len(totals) >= MIN_GLOBAL_SAMPLE:
        unusual, baseline, strength = _robust_anomaly(totals, extraction.total)
        if unusual:
            confidence = min(
                Decimal("0.9500"), Decimal("0.6500") + min(strength, Decimal("3")) / Decimal("10")
            )
            findings.append(
                FindingSpec(
                    key=f"portfolio-{extraction.currency}",
                    category=AIFindingCategory.AMOUNT,
                    severity=AIFindingSeverity.MEDIUM,
                    title="Invoice amount is unusual for recent history",
                    explanation=(
                        f"The {extraction.currency} total {_money(extraction.total)} materially "
                        f"differs from the historical median {_money(baseline)} across "
                        f"{len(totals)} invoices."
                    ),
                    evidence={
                        "sample_size": len(totals),
                        "method": "median absolute deviation",
                        "historical_median": _money(baseline),
                    },
                    affected_fields=["total"],
                    confidence=confidence.quantize(Decimal("0.0001")),
                    observed_value=_money(extraction.total),
                    expected_value=f"historical median {_money(baseline)}",
                )
            )
    vendor = _normalized(extraction.vendor_name)
    vendor_totals = [
        row.total
        for row in rows
        if row.total is not None and _normalized(row.vendor_name) == vendor
    ]
    if vendor and len(vendor_totals) >= MIN_VENDOR_SAMPLE:
        unusual, baseline, strength = _robust_anomaly(vendor_totals, extraction.total)
        if unusual:
            confidence = min(
                Decimal("0.9500"), Decimal("0.6500") + min(strength, Decimal("3")) / Decimal("10")
            )
            findings.append(
                FindingSpec(
                    key=f"vendor-{vendor}-{extraction.currency}",
                    category=AIFindingCategory.VENDOR,
                    severity=AIFindingSeverity.MEDIUM,
                    title="Vendor amount deviates from its historical pattern",
                    explanation=(
                        f"This {extraction.currency} total is materially different from the median "
                        f"{_money(baseline)} for {extraction.vendor_name} across "
                        f"{len(vendor_totals)} prior invoices."
                    ),
                    evidence={
                        "sample_size": len(vendor_totals),
                        "method": "median absolute deviation",
                        "historical_median": _money(baseline),
                    },
                    affected_fields=["vendor_name", "total"],
                    confidence=confidence.quantize(Decimal("0.0001")),
                    observed_value=_money(extraction.total),
                    expected_value=f"vendor median {_money(baseline)}",
                )
            )
    return findings


def generate_finding_specs(database: Session, document: Document) -> list[FindingSpec]:
    extraction = document.invoice_extraction
    if extraction is None:
        return [
            FindingSpec(
                key="no-invoice-extraction",
                category=AIFindingCategory.CONFIDENCE,
                severity=AIFindingSeverity.HIGH,
                title="Invoice extraction requires review",
                explanation=(
                    "No structured invoice fields were produced from the extracted document text."
                ),
                evidence={"source": "invoice extraction pipeline"},
                affected_fields=["invoice_extraction"],
            )
        ]
    findings = _missing_findings(extraction)
    findings.extend(_arithmetic_findings(extraction))
    findings.extend(_tax_findings(extraction))
    findings.extend(_date_findings(document, extraction))
    findings.extend(_duplicate_findings(database, document, extraction))
    findings.extend(_amount_findings(database, document, extraction))
    if not extraction.is_valid and extraction.validation_error:
        findings.append(
            FindingSpec(
                key="extraction-validation",
                category=AIFindingCategory.CONFIDENCE,
                severity=AIFindingSeverity.MEDIUM,
                title="Extracted values require human review",
                explanation=extraction.validation_error,
                evidence={"source": "existing extraction validation"},
                affected_fields=["invoice_extraction"],
            )
        )
    return findings


def analyze_document(database: Session, document: Document) -> list[AIFinding]:
    specs = generate_finding_specs(database, document)
    existing = {
        finding.signature: finding
        for finding in database.scalars(
            select(AIFinding).where(AIFinding.document_id == document.id)
        ).all()
    }
    results: list[AIFinding] = []
    for spec in specs:
        finding = existing.get(spec.signature)
        if finding is None:
            finding = AIFinding(document_id=document.id, signature=spec.signature)
            database.add(finding)
        finding.category = spec.category
        finding.severity = spec.severity
        finding.title = spec.title
        finding.explanation = spec.explanation
        finding.evidence = spec.evidence
        finding.affected_fields = spec.affected_fields
        finding.confidence = spec.confidence
        finding.observed_value = spec.observed_value
        finding.expected_value = spec.expected_value
        results.append(finding)
    document.ai_analysis_status = AIAnalysisStatus.COMPLETED
    document.ai_analysis_error = None
    document.ai_analyzed_at = datetime.now(UTC)
    return results
