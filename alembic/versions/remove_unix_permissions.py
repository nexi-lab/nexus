"""remove_unix_permissions_pure_rebac

Revision ID: f1234567890a
Revises: da7be77fac7c
Create Date: 2025-10-25 12:00:00.000000

This migration removes UNIX-style permissions (owner, group, mode) and ACL support
from the database schema, completing the migration to pure ReBAC in v0.6.0.

All permissions are now managed through ReBAC relationships.
"""

from collections.abc import Sequence
from contextlib import suppress
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1234567890a"
down_revision: Union[str, Sequence[str], None] = "da7be77fac7c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove UNIX permissions and ACL tables for pure ReBAC (v0.6.0)."""

    # Drop ACL table (if exists)
    with suppress(Exception):  # Index may not exist
        op.drop_index("idx_acl_entries_type_id", table_name="acl_entries")

    with suppress(Exception):  # Index may not exist
        op.drop_index("idx_acl_entries_path_id", table_name="acl_entries")

    with suppress(Exception):  # Table may not exist
        op.drop_table("acl_entries")

    # Drop UNIX permission columns from file_paths
    with suppress(Exception):  # Index may not exist
        op.drop_index("idx_file_paths_owner", table_name="file_paths")

    with suppress(Exception):  # Index may not exist
        op.drop_index("idx_file_paths_group", table_name="file_paths")

    with suppress(Exception):  # Column may not exist
        op.drop_column("file_paths", "mode")

    with suppress(Exception):  # Column may not exist
        op.drop_column("file_paths", "group")

    with suppress(Exception):  # Column may not exist
        op.drop_column("file_paths", "owner")


def downgrade() -> None:
    """Restore UNIX permissions and ACL tables (if needed for rollback)."""

    # Re-add UNIX permission columns to file_paths
    op.add_column("file_paths", sa.Column("owner", sa.String(length=255), nullable=True))
    op.add_column("file_paths", sa.Column("group", sa.String(length=255), nullable=True))
    op.add_column("file_paths", sa.Column("mode", sa.Integer(), nullable=True))
    op.create_index("idx_file_paths_owner", "file_paths", ["owner"], unique=False)
    op.create_index("idx_file_paths_group", "file_paths", ["group"], unique=False)

    # Re-create ACL table
    op.create_table(
        "acl_entries",
        sa.Column("acl_id", sa.String(length=36), nullable=False),
        sa.Column("path_id", sa.String(length=36), nullable=False),
        sa.Column("entry_type", sa.String(length=20), nullable=False),
        sa.Column("identifier", sa.String(length=255), nullable=True),
        sa.Column("permissions", sa.String(length=10), nullable=False),
        sa.Column("deny", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["path_id"], ["file_paths.path_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("acl_id"),
    )
    op.create_index("idx_acl_entries_path_id", "acl_entries", ["path_id"], unique=False)
    op.create_index(
        "idx_acl_entries_type_id", "acl_entries", ["entry_type", "identifier"], unique=False
    )
