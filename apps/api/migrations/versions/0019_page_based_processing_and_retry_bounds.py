"""Add page quantities and durable retry attempts to processing accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_operations",
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "processing_operations",
        sa.Column("retry_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "organization_monthly_usage",
        sa.Column(
            "successful_processing_pages", sa.BigInteger(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("organization_monthly_usage", "successful_processing_pages")
    op.drop_column("processing_operations", "retry_attempts")
    op.drop_column("processing_operations", "page_count")
