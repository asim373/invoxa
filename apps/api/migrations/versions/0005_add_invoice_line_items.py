"""add invoice line items

Revision ID: 0005_add_invoice_line_items
Revises: 0004_add_invoice_validation
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_add_invoice_line_items"
down_revision: str | Sequence[str] | None = "0004_add_invoice_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoice_line_items",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("invoice_document_id", sa.UUID(), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unit_price", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.Column("line_total", sa.Numeric(precision=18, scale=2), nullable=False),
        sa.ForeignKeyConstraint(
            ["invoice_document_id"], ["invoice_extractions.document_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_invoice_line_items_invoice_document_id",
        "invoice_line_items",
        ["invoice_document_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_invoice_line_items_invoice_document_id", table_name="invoice_line_items")
    op.drop_table("invoice_line_items")
