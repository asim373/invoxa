"""add extracted text to documents

Revision ID: 0002_add_extracted_text
Revises: 0001_create_documents
Create Date: 2026-08-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_extracted_text"
down_revision: str | Sequence[str] | None = "0001_create_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("extracted_text", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("documents", "extracted_text")
