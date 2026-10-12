"""Persist SCIM work email separately from immutable login identity."""

import sqlalchemy as sa
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("scim_user_mappings", sa.Column("work_email", sa.String(320), nullable=True))


def downgrade() -> None:
    op.drop_column("scim_user_mappings", "work_email")
