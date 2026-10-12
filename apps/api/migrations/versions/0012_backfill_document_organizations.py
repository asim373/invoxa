"""add document organization bridge and backfill legacy workspaces

Revision ID: 0012
Revises: 0011
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("organization_id", sa.UUID(), nullable=True))
    op.add_column("documents", sa.Column("created_by_user_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        "fk_documents_organization_id_organizations",
        "documents",
        "organizations",
        ["organization_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_documents_created_by_user_id_users",
        "documents",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_documents_organization_id", "documents", ["organization_id"], unique=False)
    op.create_index(
        "ix_documents_created_by_user_id", "documents", ["created_by_user_id"], unique=False
    )

    # Each legacy user with owned documents receives a private imported workspace.
    # The UUID-derived slug and ID are deterministic. Existing users are never
    # grouped by email domain or any other inferred company relationship.
    op.execute(
        sa.text(
            """
            DO $backfill$
            DECLARE
                legacy_user RECORD;
                imported_organization_id UUID;
                imported_slug TEXT;
            BEGIN
                FOR legacy_user IN
                    SELECT DISTINCT u.id, u.email
                    FROM users AS u
                    JOIN documents AS d ON d.owner_id = u.id
                    WHERE d.organization_id IS NULL
                LOOP
                    imported_organization_id := md5(
                        'invoxa-imported-organization:' || legacy_user.id::text
                    )::uuid;
                    imported_slug := 'imported-' || replace(legacy_user.id::text, '-', '');

                    INSERT INTO organizations (id, name, slug, status)
                    VALUES (
                        imported_organization_id,
                        left('Imported Workspace ' || legacy_user.email, 255),
                        imported_slug,
                        'active'
                    )
                    ON CONFLICT (id) DO NOTHING;

                    INSERT INTO organization_members (
                        id, organization_id, user_id, role, status, joined_at
                    )
                    VALUES (
                        md5(
                            'invoxa-imported-membership:' || legacy_user.id::text
                        )::uuid,
                        imported_organization_id,
                        legacy_user.id,
                        'owner',
                        'active',
                        now()
                    )
                    ON CONFLICT (organization_id, user_id) DO NOTHING;

                    UPDATE documents
                    SET organization_id = imported_organization_id,
                        created_by_user_id = owner_id
                    WHERE owner_id = legacy_user.id
                      AND organization_id IS NULL;
                END LOOP;
            END
            $backfill$;
            """
        )
    )


def downgrade() -> None:
    # Only remove values/structures introduced by this revision. The imported
    # organizations and memberships are intentionally retained: deleting them
    # would destroy migration-created commercial identity/data.
    op.drop_index("ix_documents_created_by_user_id", table_name="documents")
    op.drop_index("ix_documents_organization_id", table_name="documents")
    op.drop_constraint(
        "fk_documents_created_by_user_id_users", "documents", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_documents_organization_id_organizations", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "created_by_user_id")
    op.drop_column("documents", "organization_id")
