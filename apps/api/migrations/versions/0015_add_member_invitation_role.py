"""Add the commercial member role to organization invitations."""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE organization_invitation_role ADD VALUE IF NOT EXISTS 'member'"
        )


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely in-place. The introduced
    # value is intentionally retained on downgrade to avoid destructive data loss.
    pass
