"""Add organization invitations."""

# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    invitation_role = sa.Enum(
        "owner", "admin", "reviewer", "viewer", name="organization_invitation_role"
    )
    invitation_status = sa.Enum(
        "pending", "accepted", "revoked", "expired", name="organization_invitation_status"
    )
    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("invited_email", sa.String(length=320), nullable=False),
        sa.Column("role", invitation_role, nullable=False),
        sa.Column("status", invitation_status, server_default="pending", nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("invited_by_user_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
        sa.UniqueConstraint("organization_id", "invited_email", "status"),
    )
    op.create_index(
        "ix_organization_invitations_organization_id",
        "organization_invitations",
        ["organization_id"],
    )
    op.create_index(
        "ix_organization_invitations_invited_email", "organization_invitations", ["invited_email"]
    )
    op.create_index("ix_organization_invitations_status", "organization_invitations", ["status"])
    op.create_index(
        "ix_organization_invitations_invited_by_user_id",
        "organization_invitations",
        ["invited_by_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_invitations_invited_by_user_id", table_name="organization_invitations"
    )
    op.drop_index("ix_organization_invitations_status", table_name="organization_invitations")
    op.drop_index(
        "ix_organization_invitations_invited_email", table_name="organization_invitations"
    )
    op.drop_index(
        "ix_organization_invitations_organization_id", table_name="organization_invitations"
    )
    op.drop_table("organization_invitations")
    sa.Enum(name="organization_invitation_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="organization_invitation_role").drop(op.get_bind(), checkfirst=True)
