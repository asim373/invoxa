"""Add organization-specific Enterprise quota configuration."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_enterprise_quotas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("monthly_processing_limit", sa.BigInteger(), nullable=False),
        sa.Column("active_seat_limit", sa.Integer(), nullable=False),
        sa.Column("storage_bytes_limit", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "monthly_processing_limit >= 5001", name="ck_enterprise_processing_min"
        ),
        sa.CheckConstraint("active_seat_limit >= 6", name="ck_enterprise_seats_min"),
        sa.CheckConstraint(
            "storage_bytes_limit >= 11000000000", name="ck_enterprise_storage_min"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_organization_enterprise_quotas_organization_id",
        "organization_enterprise_quotas",
        ["organization_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_enterprise_quotas_organization_id",
        table_name="organization_enterprise_quotas",
    )
    op.drop_table("organization_enterprise_quotas")
