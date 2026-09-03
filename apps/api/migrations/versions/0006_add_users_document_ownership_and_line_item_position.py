"""add users, document ownership, and line item positions

Revision ID: 0006_add_users_ownership
Revises: 0005_add_invoice_line_items
Create Date: 2026-08-24

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_add_users_ownership"
down_revision: str | Sequence[str] | None = "0005_add_invoice_line_items"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.add_column("documents", sa.Column("owner_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_documents_owner_id_users",
        "documents",
        "users",
        ["owner_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_documents_owner_id", "documents", ["owner_id"], unique=False)

    op.add_column("invoice_line_items", sa.Column("position", sa.Integer(), nullable=True))
    op.execute(
        """
        WITH numbered_items AS (
            SELECT id, ROW_NUMBER() OVER (
                PARTITION BY invoice_document_id ORDER BY id
            ) - 1 AS position
            FROM invoice_line_items
        )
        UPDATE invoice_line_items
        SET position = numbered_items.position
        FROM numbered_items
        WHERE invoice_line_items.id = numbered_items.id
        """
    )
    op.alter_column("invoice_line_items", "position", nullable=False)
    op.create_unique_constraint(
        "uq_invoice_line_items_document_position",
        "invoice_line_items",
        ["invoice_document_id", "position"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_invoice_line_items_document_position", "invoice_line_items", type_="unique"
    )
    op.drop_column("invoice_line_items", "position")
    op.drop_index("ix_documents_owner_id", table_name="documents")
    op.drop_constraint("fk_documents_owner_id_users", "documents", type_="foreignkey")
    op.drop_column("documents", "owner_id")
    op.drop_table("users")
