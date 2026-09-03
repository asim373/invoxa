"""Add user roles for API authorization.

Revision ID: 0007
Revises: 0006_add_users_ownership
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006_add_users_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

user_role = sa.Enum("admin", "reviewer", "viewer", name="user_role")


def upgrade() -> None:
    user_role.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "users",
        sa.Column(
            "role",
            user_role,
            nullable=False,
            server_default="reviewer",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "role")
    user_role.drop(op.get_bind(), checkfirst=True)
