"""Add the organization-scoped SCIM foundation."""
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "organization_scim_configurations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "scim_credentials",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, nullable=False),
        sa.Column("label", sa.String(100), nullable=False),
        sa.Column("secret_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "label"),
    )
    op.create_index("ix_scim_credentials_organization_id", "scim_credentials", ["organization_id"])
    op.create_table(
        "scim_user_mappings",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("external_id", sa.String(255)),
        sa.Column("user_name", sa.String(320), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("deactivated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["organization_id", "user_id"], ["organization_members.organization_id", "organization_members.user_id"], ondelete="CASCADE"),
        sa.UniqueConstraint("organization_id", "user_id"),
        sa.UniqueConstraint("organization_id", "external_id"),
    )
    op.create_index("ix_scim_user_mappings_organization_id", "scim_user_mappings", ["organization_id"])
    op.create_index("ix_scim_user_mappings_user_id", "scim_user_mappings", ["user_id"])
    op.create_table(
        "scim_provisioning_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("organization_id", uuid, nullable=False),
        sa.Column("credential_id", uuid),
        sa.Column("resource_id", uuid),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["credential_id"], ["scim_credentials.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_scim_provisioning_events_organization_id", "scim_provisioning_events", ["organization_id"])
    op.create_index("ix_scim_provisioning_events_credential_id", "scim_provisioning_events", ["credential_id"])


def downgrade() -> None:
    op.drop_table("scim_provisioning_events")
    op.drop_index("ix_scim_user_mappings_user_id", table_name="scim_user_mappings")
    op.drop_index("ix_scim_user_mappings_organization_id", table_name="scim_user_mappings")
    op.drop_table("scim_user_mappings")
    op.drop_index("ix_scim_credentials_organization_id", table_name="scim_credentials")
    op.drop_table("scim_credentials")
    op.drop_table("organization_scim_configurations")
