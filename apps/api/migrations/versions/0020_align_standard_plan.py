"""Align the legacy Starter plan code with the canonical Standard plan."""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE organizations SET plan_code = 'standard' WHERE plan_code = 'starter'")
    op.alter_column(
        "organizations",
        "plan_code",
        existing_type=sa.String(length=50),
        server_default="standard",
    )


def downgrade() -> None:
    op.execute("UPDATE organizations SET plan_code = 'starter' WHERE plan_code = 'standard'")
    op.alter_column(
        "organizations",
        "plan_code",
        existing_type=sa.String(length=50),
        server_default="starter",
    )
