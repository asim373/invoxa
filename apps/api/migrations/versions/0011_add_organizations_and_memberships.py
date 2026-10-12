"""add organizations and organization memberships

Revision ID: 0011
Revises: 0010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


organization_status = sa.Enum("active", "inactive", name="organization_status")
organization_member_role = sa.Enum(
    "owner", "admin", "reviewer", "viewer", name="organization_member_role"
)
organization_member_status = sa.Enum(
    "pending", "active", "inactive", name="organization_member_status"
)


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("status", organization_status, server_default="active", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_index("ix_organizations_status", "organizations", ["status"], unique=False)

    op.create_table(
        "organization_members",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("organization_id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("role", organization_member_role, server_default="viewer", nullable=False),
        sa.Column("status", organization_member_status, server_default="pending", nullable=False),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "user_id"),
    )
    op.create_index(
        "ix_organization_members_organization_id",
        "organization_members",
        ["organization_id"],
        unique=False,
    )
    op.create_index(
        "ix_organization_members_user_id", "organization_members", ["user_id"], unique=False
    )
    op.create_index(
        "ix_organization_members_status", "organization_members", ["status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_organization_members_status", table_name="organization_members")
    op.drop_index("ix_organization_members_user_id", table_name="organization_members")
    op.drop_index("ix_organization_members_organization_id", table_name="organization_members")
    op.drop_table("organization_members")
    op.drop_index("ix_organizations_status", table_name="organizations")
    op.drop_table("organizations")
    bind = op.get_bind()
    organization_member_status.drop(bind, checkfirst=True)
    organization_member_role.drop(bind, checkfirst=True)
    organization_status.drop(bind, checkfirst=True)
