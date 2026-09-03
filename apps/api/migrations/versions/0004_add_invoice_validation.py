"""add invoice extraction validation state

Revision ID: 0004_add_invoice_validation
Revises: 0003_add_invoice_extractions
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_invoice_validation"
down_revision: str | Sequence[str] | None = "0003_add_invoice_extractions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "invoice_extractions",
        sa.Column("is_valid", sa.Boolean(), server_default=sa.true(), nullable=False),
    )
    op.add_column("invoice_extractions", sa.Column("validation_error", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("invoice_extractions", "validation_error")
    op.drop_column("invoice_extractions", "is_valid")
