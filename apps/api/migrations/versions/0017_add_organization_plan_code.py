"""Add provider-neutral organization plan assignment."""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("plan_code", sa.String(length=50), nullable=True))
    op.execute("UPDATE organizations SET plan_code = 'starter' WHERE plan_code IS NULL")
    op.alter_column(
        "organizations",
        "plan_code",
        existing_type=sa.String(length=50),
        nullable=False,
        server_default="starter",
    )
    op.create_index("ix_organizations_plan_code", "organizations", ["plan_code"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_organizations_plan_code", table_name="organizations")
    op.drop_column("organizations", "plan_code")
