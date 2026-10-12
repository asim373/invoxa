"""Track voluntary deactivation separately from administrative disabling."""

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("self_deactivated_at", sa.DateTime(timezone=True)))
    op.create_table(
        "account_reactivation_tokens",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id", sa.UUID(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_hash", sa.String(64), unique=True, nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_account_reactivation_tokens_user_id", "account_reactivation_tokens", ["user_id"]
    )


def downgrade() -> None:
    op.drop_table("account_reactivation_tokens")
    op.drop_column("users", "self_deactivated_at")
