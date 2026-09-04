"""Add persisted AI Analysis findings and document analysis state.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

analysis_status = postgresql.ENUM(
    "pending", "completed", "failed", name="ai_analysis_status", create_type=False
)
finding_category = postgresql.ENUM(
    "arithmetic_anomaly",
    "duplicate_invoice",
    "unusual_amount",
    "vendor_pattern",
    "missing_information",
    "low_confidence_extraction",
    "date_anomaly",
    "tax_consistency",
    name="ai_finding_category",
    create_type=False,
)
finding_severity = postgresql.ENUM(
    "low", "medium", "high", "critical", name="ai_finding_severity", create_type=False
)
finding_status = postgresql.ENUM(
    "open", "acknowledged", "resolved", name="ai_finding_status", create_type=False
)


def upgrade() -> None:
    analysis_status.create(op.get_bind(), checkfirst=True)
    finding_category.create(op.get_bind(), checkfirst=True)
    finding_severity.create(op.get_bind(), checkfirst=True)
    finding_status.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "documents",
        sa.Column("ai_analysis_status", analysis_status, server_default="pending", nullable=False),
    )
    op.add_column("documents", sa.Column("ai_analysis_error", sa.Text(), nullable=True))
    op.add_column(
        "documents", sa.Column("ai_analyzed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_table(
        "ai_findings",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("document_id", sa.UUID(), nullable=False),
        sa.Column("category", finding_category, nullable=False),
        sa.Column("severity", finding_severity, nullable=False),
        sa.Column("status", finding_status, server_default="open", nullable=False),
        sa.Column("signature", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column("affected_fields", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        sa.Column("observed_value", sa.String(length=255), nullable=True),
        sa.Column("expected_value", sa.String(length=255), nullable=True),
        sa.Column("reviewed_by_id", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "signature"),
    )
    op.create_index("ix_ai_findings_document_id", "ai_findings", ["document_id"])
    op.create_index("ix_ai_findings_category", "ai_findings", ["category"])
    op.create_index("ix_ai_findings_severity", "ai_findings", ["severity"])
    op.create_index("ix_ai_findings_status", "ai_findings", ["status"])


def downgrade() -> None:
    op.drop_index("ix_ai_findings_status", table_name="ai_findings")
    op.drop_index("ix_ai_findings_severity", table_name="ai_findings")
    op.drop_index("ix_ai_findings_category", table_name="ai_findings")
    op.drop_index("ix_ai_findings_document_id", table_name="ai_findings")
    op.drop_table("ai_findings")
    op.drop_column("documents", "ai_analyzed_at")
    op.drop_column("documents", "ai_analysis_error")
    op.drop_column("documents", "ai_analysis_status")
    finding_status.drop(op.get_bind(), checkfirst=True)
    finding_severity.drop(op.get_bind(), checkfirst=True)
    finding_category.drop(op.get_bind(), checkfirst=True)
    analysis_status.drop(op.get_bind(), checkfirst=True)
