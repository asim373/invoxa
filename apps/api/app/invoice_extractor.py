import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from apps.api.app.models import InvoiceExtraction, InvoiceLineItem

LABELS = {
    "invoice_number": r"invoice\s*(?:number|no\.?|#)\s*[:#-]?\s*([^\n]+)",
    "vendor_name": r"(?:vendor|seller|from)\s*[:#-]?\s*([^\n]+)",
    "customer_name": r"(?:customer|bill\s*to|billed\s*to)\s*[:#-]?\s*([^\n]+)",
}
MONEY_LABELS = {
    "subtotal": r"subtotal\s*[:\-]?\s*([^\n]+)",
    "tax": r"(?:tax|vat)\s*[:\-]?\s*([^\n]+)",
    "total": r"(?:grand\s+total|total\s+due|total)\s*[:\-]?\s*([^\n]+)",
}
LINE_ITEM_PATTERN = re.compile(
    r"^\s*(?P<description>[^|\n]+?)\s*\|\s*"
    r"(?P<quantity>\d+(?:\.\d+)?)\s*\|\s*"
    r"(?P<unit_price>[-+]?\d[\d,]*(?:\.\d{1,2})?)\s*\|\s*"
    r"(?P<line_total>[-+]?\d[\d,]*(?:\.\d{1,2})?)\s*$"
)
WHITESPACE_LINE_ITEM_PATTERN = re.compile(
    r"^\s*(?P<description>\D\S(?:.*?\S)?)\s{2,}"
    r"(?P<quantity>\d+(?:\.\d+)?)\s{2,}"
    r"(?P<unit_price>[-+]?\d[\d,]*(?:\.\d{1,2})?)\s{2,}"
    r"(?P<line_total>[-+]?\d[\d,]*(?:\.\d{1,2})?)\s*$"
)
CURRENCY_CODES = {"USD", "EUR", "GBP", "INR", "CAD", "AUD", "JPY"}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}
MATERIAL_AMOUNT_TOLERANCE = Decimal("0.01")


def extract_invoice_fields(text: str) -> InvoiceExtraction:
    values = {field: _label_value(text, pattern) for field, pattern in LABELS.items()}
    amounts = {field: _extract_money(text, pattern) for field, pattern in MONEY_LABELS.items()}
    validation_error = _validate_totals(amounts["subtotal"], amounts["tax"], amounts["total"])
    line_items = _extract_line_items(text)
    validation_error = validation_error or _validate_line_items(line_items, amounts["subtotal"])
    return InvoiceExtraction(
        invoice_number=_normalize_text(values["invoice_number"]),
        invoice_date=_extract_date(text),
        vendor_name=_normalize_text(values["vendor_name"]),
        customer_name=_normalize_text(values["customer_name"]),
        currency=_extract_currency(text),
        subtotal=amounts["subtotal"],
        tax=amounts["tax"],
        total=amounts["total"],
        is_valid=validation_error is None,
        validation_error=validation_error,
        line_items=line_items,
    )


def _label_value(text: str, pattern: str) -> str | None:
    match = re.search(rf"^\s*{pattern}", text, re.IGNORECASE | re.MULTILINE)
    return match.group(1).strip() if match else None


def _normalize_text(value: str | None) -> str | None:
    return re.sub(r"\s+", " ", value).strip() if value is not None else None


def _extract_currency(text: str) -> str | None:
    for code in sorted(CURRENCY_CODES):
        if re.search(rf"\b{code}\b", text, re.IGNORECASE):
            return code
    for symbol, code in CURRENCY_SYMBOLS.items():
        if symbol in text:
            return code
    return None


def _extract_money(text: str, pattern: str) -> Decimal | None:
    value = _label_value(text, pattern)
    if value is None:
        return None
    match = re.search(r"[-+]?\d[\d,]*(?:\.\d{1,2})?", value)
    if match is None:
        return None
    try:
        return Decimal(match.group(0).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _extract_date(text: str) -> date | None:
    value = _label_value(text, r"invoice\s*date\s*[:\-]?\s*([^\n]+)")
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{1,2}[/-]\d{1,2}[/-]\d{4}", value):
        day, month, _ = (int(part) for part in re.split(r"[/-]", value))
        if day <= 12 and month <= 12:
            return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            continue
    return None


def _validate_totals(
    subtotal: Decimal | None, tax: Decimal | None, total: Decimal | None
) -> str | None:
    if subtotal is None or tax is None or total is None:
        return None
    if abs((subtotal + tax) - total) > MATERIAL_AMOUNT_TOLERANCE:
        return "Subtotal plus tax does not match total."
    return None


def _extract_line_items(text: str) -> list[InvoiceLineItem]:
    items: list[InvoiceLineItem] = []
    for line in text.splitlines():
        match = LINE_ITEM_PATTERN.match(line) or WHITESPACE_LINE_ITEM_PATTERN.match(line)
        if match is None:
            continue
        values = match.groupdict()
        quantity = _parse_decimal(values["quantity"], scale="0.0001")
        unit_price = _parse_decimal(values["unit_price"], scale="0.01")
        line_total = _parse_decimal(values["line_total"], scale="0.01")
        description = _normalize_text(values["description"])
        if quantity is None or unit_price is None or line_total is None or description is None:
            continue
        if abs((quantity * unit_price) - line_total) > MATERIAL_AMOUNT_TOLERANCE:
            continue
        items.append(
            InvoiceLineItem(
                description=description,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    return items


def _parse_decimal(value: str, scale: str) -> Decimal | None:
    try:
        return Decimal(value.replace(",", "")).quantize(Decimal(scale))
    except InvalidOperation:
        return None


def _validate_line_items(line_items: list[InvoiceLineItem], subtotal: Decimal | None) -> str | None:
    if not line_items or subtotal is None:
        return None
    line_total = sum((item.line_total for item in line_items), Decimal("0.00"))
    if abs(line_total - subtotal) > MATERIAL_AMOUNT_TOLERANCE:
        return "Line item totals do not match subtotal."
    return None
