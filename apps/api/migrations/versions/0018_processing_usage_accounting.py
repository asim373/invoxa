"""Add durable processing operations and monthly usage accounting."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    operation_status = postgresql.ENUM(
        "queued", "processing", "completed", "failed", name="processing_operation_status"
    )
    operation_status.create(op.get_bind(), checkfirst=True)
    operation_status_column = postgresql.ENUM(
        "queued",
        "processing",
        "completed",
        "failed",
        name="processing_operation_status",
        create_type=False,
    )
    op.create_table(
        "processing_operations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=True),
        sa.Column("status", operation_status_column, nullable=False, server_default="queued"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_details", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_processing_operations_document_id", "processing_operations", ["document_id"]
    )
    op.create_index(
        "ix_processing_operations_organization_id", "processing_operations", ["organization_id"]
    )
    op.create_index("ix_processing_operations_status", "processing_operations", ["status"])
    op.create_table(
        "organization_monthly_usage",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("usage_month", sa.Date(), nullable=False),
        sa.Column(
            "successful_processing_count", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "usage_month"),
    )
    op.create_index(
        "ix_organization_monthly_usage_organization_id",
        "organization_monthly_usage",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_monthly_usage_usage_month", "organization_monthly_usage", ["usage_month"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_monthly_usage_usage_month", table_name="organization_monthly_usage"
    )
    op.drop_index(
        "ix_organization_monthly_usage_organization_id", table_name="organization_monthly_usage"
    )
    op.drop_table("organization_monthly_usage")
    op.drop_index("ix_processing_operations_status", table_name="processing_operations")
    op.drop_index("ix_processing_operations_organization_id", table_name="processing_operations")
    op.drop_index("ix_processing_operations_document_id", table_name="processing_operations")
    op.drop_table("processing_operations")
    postgresql.ENUM(name="processing_operation_status").drop(op.get_bind(), checkfirst=True)
