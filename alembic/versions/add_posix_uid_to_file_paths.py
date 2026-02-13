"""Add posix_uid to file_paths for O(1) owner permission checks

Revision ID: add_posix_uid
Revises: tiger_cache_remove_tenant
Create Date: 2025-01-01

Issue #920: POSIX Mode Bits for O(1) Permission Checks

This migration adds posix_uid column to file_paths table to enable
fast O(1) ownership verification without ReBAC graph traversal.

When posix_uid is set and matches the requesting user, permission
checks can bypass the expensive ReBAC lookup entirely.

Changes:
- Add posix_uid column (nullable VARCHAR(255))
- Add index for owner-based queries (e.g., "list my files")
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_posix_uid"
down_revision: Union[str, Sequence[str], None] = "tiger_cache_remove_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add posix_uid column to file_paths table."""
    bind = op.get_bind()

    # Add posix_uid column (nullable for backward compatibility)
    op.add_column(
        "file_paths",
        sa.Column("posix_uid", sa.String(255), nullable=True),
    )

    # Add index for owner-based queries
    op.create_index(
        "idx_file_paths_posix_uid",
        "file_paths",
        ["posix_uid"],
    )

    # Backfill posix_uid from ReBAC direct_owner relationships
    # This enables O(1) owner permission checks immediately after migration
    if bind.dialect.name == "postgresql":
        bind.execute(
            sa.text("""
            UPDATE file_paths fp
            SET posix_uid = rt.subject_id
            FROM rebac_tuples rt
            WHERE rt.object_id = fp.virtual_path
              AND fp.posix_uid IS NULL
              AND fp.deleted_at IS NULL
              AND rt.relation = 'direct_owner'
              AND rt.object_type = 'file'
        """)
        )
    else:
        # SQLite: Subquery-based update
        bind.execute(
            sa.text("""
            UPDATE file_paths
            SET posix_uid = (
                SELECT rt.subject_id
                FROM rebac_tuples rt
                WHERE rt.object_id = file_paths.virtual_path
                  AND rt.relation = 'direct_owner'
                  AND rt.object_type = 'file'
                LIMIT 1
            )
            WHERE posix_uid IS NULL
              AND deleted_at IS NULL
              AND EXISTS (
                SELECT 1 FROM rebac_tuples rt
                WHERE rt.object_id = file_paths.virtual_path
                  AND rt.relation = 'direct_owner'
                  AND rt.object_type = 'file'
              )
        """)
        )


def downgrade() -> None:
    """Remove posix_uid column from file_paths table."""
    from contextlib import suppress

    from sqlalchemy.exc import OperationalError, ProgrammingError

    # Index may have been lost during batch_alter_table operations in later
    # migrations that recreate the file_paths table.
    with suppress(OperationalError, ProgrammingError):
        op.drop_index("idx_file_paths_posix_uid", table_name="file_paths")

    with op.batch_alter_table("file_paths") as batch_op:
        batch_op.drop_column("posix_uid")
