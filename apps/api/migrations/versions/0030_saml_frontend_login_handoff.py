"""Persist organization scope for one-time SAML frontend login grants."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "oauth_login_codes",
        sa.Column("sso_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_oauth_login_codes_sso_organization_id_organizations",
        "oauth_login_codes",
        "organizations",
        ["sso_organization_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_oauth_login_codes_sso_organization_id",
        "oauth_login_codes",
        ["sso_organization_id"],
    )


def downgrade() -> None:
    # One-time SAML grants cannot safely survive rollback to a version that
    # treats every OAuthLoginCode as a Google code.
    op.execute("DELETE FROM oauth_login_codes WHERE sso_organization_id IS NOT NULL")
    op.drop_index("ix_oauth_login_codes_sso_organization_id", table_name="oauth_login_codes")
    op.drop_constraint(
        "fk_oauth_login_codes_sso_organization_id_organizations",
        "oauth_login_codes",
        type_="foreignkey",
    )
    op.drop_column("oauth_login_codes", "sso_organization_id")
