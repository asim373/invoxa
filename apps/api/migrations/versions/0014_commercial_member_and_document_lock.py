"""Add commercial member role and document finalization metadata."""

# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE organization_member_role ADD VALUE IF NOT EXISTS 'member'"
        )
    op.add_column(
        "documents",
        sa.Column("is_finalized", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("documents", sa.Column("finalized_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("finalized_by_user_id", sa.UUID(), nullable=True))
    op.add_column("documents", sa.Column("reopened_at", sa.DateTime(timezone=True)))
    op.add_column("documents", sa.Column("reopened_by_user_id", sa.UUID(), nullable=True))
    op.create_index("ix_documents_is_finalized", "documents", ["is_finalized"])
    op.create_index("ix_documents_finalized_by_user_id", "documents", ["finalized_by_user_id"])
    op.create_index("ix_documents_reopened_by_user_id", "documents", ["reopened_by_user_id"])
    op.create_foreign_key(
        "fk_documents_finalized_by_user_id_users",
        "documents",
        "users",
        ["finalized_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_documents_reopened_by_user_id_users",
        "documents",
        "users",
        ["reopened_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint("fk_documents_reopened_by_user_id_users", "documents", type_="foreignkey")
    op.drop_constraint("fk_documents_finalized_by_user_id_users", "documents", type_="foreignkey")
    op.drop_index("ix_documents_reopened_by_user_id", table_name="documents")
    op.drop_index("ix_documents_finalized_by_user_id", table_name="documents")
    op.drop_index("ix_documents_is_finalized", table_name="documents")
    op.drop_column("documents", "reopened_by_user_id")
    op.drop_column("documents", "reopened_at")
    op.drop_column("documents", "finalized_by_user_id")
    op.drop_column("documents", "finalized_at")
    op.drop_column("documents", "is_finalized")
