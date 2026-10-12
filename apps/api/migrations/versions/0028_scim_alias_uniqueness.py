"""Enforce case-insensitive organization-scoped SCIM alias uniqueness."""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing collisions fail the migration; never rewrite identities silently.
    op.create_index(
        "uq_scim_org_username_ci",
        "scim_user_mappings",
        ["organization_id", sa.text("lower(user_name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("uq_scim_org_username_ci", table_name="scim_user_mappings")
