"""Add optional profile name and pending login-email ownership changes."""

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("display_name", sa.String(120)))
    op.create_table(
        "email_change_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("old_email", sa.String(320), nullable=False),
        sa.Column("new_email", sa.String(320), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_email_change_tokens_user_id", "email_change_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_table("email_change_tokens")
    op.drop_column("users", "display_name")
