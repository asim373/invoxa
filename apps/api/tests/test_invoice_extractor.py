from datetime import date
from decimal import Decimal

from apps.api.app.invoice_extractor import extract_invoice_fields


def test_extracts_complete_invoice_text() -> None:
    result = extract_invoice_fields(
        """Invoice Number: INV-1001
Invoice Date: 2026-08-23
Vendor: Acme Supplies
Customer: Example Retail
Currency: USD
Subtotal: USD 1,200.50
Tax: USD 240.10
Total: USD 1,440.60"""
    )

    assert result.invoice_number == "INV-1001"
    assert result.invoice_date == date(2026, 8, 23)
    assert result.vendor_name == "Acme Supplies"
    assert result.customer_name == "Example Retail"
    assert result.currency == "USD"
    assert result.subtotal == Decimal("1200.50")
    assert result.tax == Decimal("240.10")
    assert result.total == Decimal("1440.60")


def test_missing_fields_are_none() -> None:
    result = extract_invoice_fields("Invoice Number: INV-2\nTotal: $20.00")

    assert result.invoice_number == "INV-2"
    assert result.total == Decimal("20.00")
    assert result.vendor_name is None
    assert result.customer_name is None
    assert result.invoice_date is None
    assert result.subtotal is None
    assert result.tax is None


def test_extracts_currency_and_money_values() -> None:
    result = extract_invoice_fields("Currency: EUR\nSubtotal: €10.5\nTax: €2\nTotal: €12.50")

    assert result.currency == "EUR"
    assert result.subtotal == Decimal("10.50")
    assert result.tax == Decimal("2.00")
    assert result.total == Decimal("12.50")


def test_extracts_supported_unambiguous_date_format() -> None:
    result = extract_invoice_fields("Invoice Date: 23/08/2026")

    assert result.invoice_date == date(2026, 8, 23)


def test_malformed_or_unstructured_text_returns_nullable_result() -> None:
    result = extract_invoice_fields("This is not a structured invoice.")

    assert result.invoice_number is None
    assert result.invoice_date is None
    assert result.currency is None
    assert result.total is None


def test_normalizes_currency_and_whitespace() -> None:
    result = extract_invoice_fields(
        "Invoice Number:   INV-3  \nVendor:  Acme   Supplies  \nCurrency: usd\nTotal: $10"
    )

    assert result.invoice_number == "INV-3"
    assert result.vendor_name == "Acme Supplies"
    assert result.currency == "USD"


def test_normalizes_euro_currency_and_decimal_values() -> None:
    result = extract_invoice_fields("Currency: eur\nSubtotal: €1,234.5\nTax: €2.1\nTotal: €1,236.6")

    assert result.currency == "EUR"
    assert result.subtotal == Decimal("1234.50")
    assert result.tax == Decimal("2.10")
    assert result.total == Decimal("1236.60")


def test_rejects_materially_inconsistent_totals() -> None:
    result = extract_invoice_fields("Subtotal: 100\nTax: 20\nTotal: 125")

    assert result.is_valid is False
    assert result.validation_error == "Subtotal plus tax does not match total."


def test_accepts_totals_within_rounding_tolerance() -> None:
    result = extract_invoice_fields("Subtotal: 100.00\nTax: 20.00\nTotal: 120.01")

    assert result.is_valid is True
    assert result.validation_error is None


def test_does_not_guess_ambiguous_date() -> None:
    result = extract_invoice_fields("Invoice Date: 01/02/2026")

    assert result.invoice_date is None


def test_extracts_multiple_pipe_line_items() -> None:
    result = extract_invoice_fields("Product A | 2 | 10.00 | 20.00\nProduct B | 3 | 5.50 | 16.50")

    assert len(result.line_items) == 2
    assert result.line_items[0].description == "Product A"
    assert result.line_items[0].quantity == Decimal("2.0000")
    assert result.line_items[0].unit_price == Decimal("10.00")
    assert result.line_items[0].line_total == Decimal("20.00")
    assert result.line_items[1].line_total == Decimal("16.50")


def test_extracts_whitespace_separated_line_item() -> None:
    result = extract_invoice_fields("Product B  3  5.50  16.50")

    assert len(result.line_items) == 1
    assert result.line_items[0].description == "Product B"
    assert result.line_items[0].quantity == Decimal("3.0000")
    assert result.line_items[0].unit_price == Decimal("5.50")
    assert result.line_items[0].line_total == Decimal("16.50")


def test_skips_malformed_and_ambiguous_line_items() -> None:
    result = extract_invoice_fields(
        "Product A | two | 10.00 | 20.00\nProduct B | 2 | 10.00 | unknown"
    )

    assert result.line_items == []


def test_validates_line_item_totals_against_subtotal() -> None:
    result = extract_invoice_fields(
        "Product A | 2 | 10.00 | 20.00\nProduct B | 3 | 5.00 | 15.00\nSubtotal: 35.00"
    )

    assert len(result.line_items) == 2
    assert result.is_valid is True


def test_marks_line_item_subtotal_mismatch_invalid() -> None:
    result = extract_invoice_fields("Product A | 2 | 10.00 | 20.00\nSubtotal: 25.00")

    assert len(result.line_items) == 1
    assert result.is_valid is False
    assert result.validation_error == "Line item totals do not match subtotal."


def test_empty_invoice_has_no_line_items() -> None:
    result = extract_invoice_fields("Invoice Number: INV-4\nSubtotal: 0.00")

    assert result.line_items == []
