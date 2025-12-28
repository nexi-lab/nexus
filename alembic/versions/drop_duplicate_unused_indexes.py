"""Drop duplicate and unused indexes for performance optimization

Based on PgHero analysis, these indexes are safe to drop:
1. idx_tenant_path_prefix - Exact duplicate of idx_file_paths_tenant_path
2. idx_rebac_subject - Covered by idx_rebac_permission_check (backward compat only)
3. idx_rebac_object - Covered by idx_rebac_object_expand (backward compat only)

This migration saves ~6MB of index storage and improves write performance.

Revision ID: drop_duplicate_unused_indexes
Revises: add_cross_tenant_share_index
Create Date: 2025-12-28
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "drop_duplicate_unused_indexes"
down_revision: Union[str, Sequence[str], None] = "add_cross_tenant_share_index"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Drop duplicate and unused indexes.

    These indexes are safe to drop because:
    - idx_tenant_path_prefix: Duplicate of idx_file_paths_tenant_path (same columns)
    - idx_rebac_subject: Covered by idx_rebac_permission_check composite index
    - idx_rebac_object: Covered by idx_rebac_object_expand composite index
    """
    # Drop duplicate index on file_paths
    op.execute(text("DROP INDEX IF EXISTS idx_tenant_path_prefix"))

    # Drop backward-compatibility indexes on rebac_tuples
    # These are superseded by comprehensive composite indexes from Issue #591
    op.execute(text("DROP INDEX IF EXISTS idx_rebac_subject"))
    op.execute(text("DROP INDEX IF EXISTS idx_rebac_object"))


def downgrade() -> None:
    """Recreate indexes if needed for rollback."""
    # Recreate idx_tenant_path_prefix on file_paths
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_tenant_path_prefix "
            "ON file_paths (tenant_id, virtual_path)"
        )
    )

    # Recreate backward-compatibility indexes on rebac_tuples
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_rebac_subject "
            "ON rebac_tuples (subject_type, subject_id)"
        )
    )
    op.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_rebac_object "
            "ON rebac_tuples (object_type, object_id)"
        )
    )
