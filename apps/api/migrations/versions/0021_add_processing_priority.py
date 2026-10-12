"""Add durable processing priority to processing operations."""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "processing_operations",
        sa.Column("priority", sa.String(length=16), nullable=False, server_default="normal"),
    )
    op.create_index(
        "ix_processing_operations_priority", "processing_operations", ["priority"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_processing_operations_priority", table_name="processing_operations")
    op.drop_column("processing_operations", "priority")
