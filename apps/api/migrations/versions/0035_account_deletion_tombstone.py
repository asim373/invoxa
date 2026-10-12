"""Add irreversible account-deletion markers without modifying existing data."""

import sqlalchemy as sa
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True)))
    op.create_index("ix_users_deleted_at", "users", ["deleted_at"])
    op.create_table(
        "account_deletion_tombstones",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column(
            "user_id",
            sa.UUID(),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "completed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_account_deletion_tombstones_completed_at",
        "account_deletion_tombstones",
        ["completed_at"],
    )


def downgrade() -> None:
    op.drop_table("account_deletion_tombstones")
    op.drop_index("ix_users_deleted_at", table_name="users")
    op.drop_column("users", "deleted_at")
