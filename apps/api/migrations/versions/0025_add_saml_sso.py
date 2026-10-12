"""Add organization-scoped SAML SSO state."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "organization_saml_configurations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("idp_entity_id", sa.String(2048), nullable=False),
        sa.Column("idp_sso_url", sa.String(2048), nullable=False),
        sa.Column("signing_certificates", sa.JSON(), nullable=False),
        sa.Column("attribute_mappings", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("id", "organization_id"),
    )
    op.create_table(
        "saml_auth_transactions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("relay_state", sa.String(128), nullable=False),
        sa.Column("request_id", sa.String(255), nullable=False),
        sa.Column("browser_hash", sa.String(64), nullable=False),
        sa.Column("configuration_digest", sa.String(64), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("return_path", sa.String(512), nullable=False, server_default="/"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["organization_saml_configurations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id", "organization_id"],
            [
                "organization_saml_configurations.id",
                "organization_saml_configurations.organization_id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "saml_assertion_replays",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("assertion_id", sa.String(255), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "consumed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["organization_saml_configurations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id", "organization_id"],
            [
                "organization_saml_configurations.id",
                "organization_saml_configurations.organization_id",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "saml_identities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("configuration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject", sa.String(1024), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(
            ["configuration_id"], ["organization_saml_configurations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("configuration_id", "subject"),
        sa.UniqueConstraint("user_id", "configuration_id"),
    )
    for table, columns in (
        ("saml_auth_transactions", ["organization_id", "configuration_id", "expires_at"]),
        ("saml_assertion_replays", ["organization_id", "configuration_id", "expires_at"]),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])
    for table, column in (
        ("saml_auth_transactions", "relay_state"),
        ("saml_auth_transactions", "request_id"),
        ("saml_assertion_replays", "assertion_id"),
    ):
        op.create_index(f"ix_{table}_{column}", table, [column], unique=True)
    for column in ("configuration_id", "user_id"):
        op.create_index(f"ix_saml_identities_{column}", "saml_identities", [column])


def downgrade() -> None:
    op.drop_table("saml_identities")
    for table, columns in (
        ("saml_assertion_replays", ["expires_at", "configuration_id", "organization_id"]),
        ("saml_auth_transactions", ["expires_at", "configuration_id", "organization_id"]),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
    op.drop_table("saml_assertion_replays")
    op.drop_table("saml_auth_transactions")
    op.drop_table("organization_saml_configurations")
