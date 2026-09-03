"""add invoice extraction results

Revision ID: 0003_add_invoice_extractions
Revises: 0002_add_extracted_text
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_add_invoice_extractions"
down_revision: str | Sequence[str] | None = "0002_add_extracted_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_extractions",
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("invoice_number", sa.String(length=255), nullable=True),
        sa.Column("invoice_date", sa.Date(), nullable=True),
        sa.Column("vendor_name", sa.String(length=255), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=True),
        sa.Column("subtotal", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("tax", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.Column("total", sa.Numeric(precision=18, scale=2), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("document_id"),
    )


def downgrade() -> None:
    op.drop_table("invoice_extractions")
