"""Add encrypted durable email delivery records."""

import sqlalchemy as sa
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transactional_emails",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "organization_id", sa.UUID(), sa.ForeignKey("organizations.id", ondelete="CASCADE")
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("related_token_id", sa.UUID()),
        sa.Column("user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE")),
        sa.Column("encrypted_message", sa.Text()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("claim_id", sa.UUID()),
        sa.Column("last_error", sa.String(64)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_transactional_emails_user_id", "transactional_emails", ["user_id"])
    op.create_index(
        "ix_transactional_emails_organization_id", "transactional_emails", ["organization_id"]
    )
    op.create_index(
        "ix_transactional_emails_due", "transactional_emails", ["status", "next_attempt_at"]
    )


def downgrade() -> None:
    op.drop_table("transactional_emails")
