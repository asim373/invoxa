"""Add enterprise SSO policy, persisted sessions and security audit events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("sso_enforced", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column(
        "organizations", sa.Column("sso_recovery_code_hash", sa.String(length=64), nullable=True)
    )
    op.create_check_constraint(
        "ck_organization_sso_enforcement_recovery",
        "organizations",
        "NOT sso_enforced OR sso_recovery_code_hash IS NOT NULL",
    )
    op.create_table(
        "user_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("auth_version", sa.Integer(), nullable=False),
        sa.Column("sso_organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["sso_organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_user_sessions_user_id", "user_sessions", ["user_id"])
    op.create_index(
        "ix_user_sessions_sso_organization_id", "user_sessions", ["sso_organization_id"]
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])
    op.create_index("ix_user_sessions_revoked_at", "user_sessions", ["revoked_at"])
    op.create_table(
        "organization_security_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("subject_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("outcome", sa.String(length=24), server_default="success", nullable=False),
        sa.Column("resource_type", sa.String(length=80), nullable=True),
        sa.Column("resource_id", sa.String(length=128), nullable=True),
        sa.Column("context", sa.JSON(), server_default="{}", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("organization_id", "actor_user_id", "subject_user_id", "created_at"):
        op.create_index(
            f"ix_org_security_audit_{column}", "organization_security_audit_events", [column]
        )


def downgrade() -> None:
    for column in ("created_at", "subject_user_id", "actor_user_id", "organization_id"):
        op.drop_index(
            f"ix_org_security_audit_{column}", table_name="organization_security_audit_events"
        )
    op.drop_table("organization_security_audit_events")
    for index in (
        "ix_user_sessions_revoked_at",
        "ix_user_sessions_expires_at",
        "ix_user_sessions_sso_organization_id",
        "ix_user_sessions_user_id",
    ):
        op.drop_index(index, table_name="user_sessions")
    op.drop_table("user_sessions")
    op.drop_constraint(
        "ck_organization_sso_enforcement_recovery", "organizations", type_="check"
    )
    op.drop_column("organizations", "sso_recovery_code_hash")
    op.drop_column("organizations", "sso_enforced")
