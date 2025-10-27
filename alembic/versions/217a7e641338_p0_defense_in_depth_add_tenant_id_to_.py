"""p0_defense_in_depth_add_tenant_id_to_file_paths

Revision ID: 217a7e641338
Revises: 3fa6791173bc
Create Date: 2025-10-26 13:58:37.593047

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "217a7e641338"
down_revision: Union[str, Sequence[str], None] = "3fa6791173bc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add tenant_id to file_paths for defense-in-depth security.

    P0 SECURITY FIX: Add database-level tenant filtering.

    Previous architecture (v0.5.0) removed tenant_id from file_paths,
    relying solely on ReBAC for multi-tenant isolation. This created
    a single point of failure.

    This migration restores defense-in-depth by:
    1. Adding tenant_id column (nullable for backward compatibility)
    2. Adding index for efficient tenant-scoped queries
    3. Updating unique constraint to tenant_id + virtual_path

    Note: Column is nullable initially. Production deployments should:
    1. Run backfill to populate tenant_id from ReBAC relationships
    2. Make column NOT NULL after backfill
    3. Drop old uq_virtual_path constraint
    """

    # Add tenant_id column (nullable for backward compatibility)
    op.add_column("file_paths", sa.Column("tenant_id", sa.String(255), nullable=True))

    # Add index for tenant-scoped queries
    op.create_index("idx_file_paths_tenant", "file_paths", ["tenant_id"])

    # Add composite index for tenant + path lookups (most common query)
    op.create_index("idx_file_paths_tenant_path", "file_paths", ["tenant_id", "virtual_path"])

    # Add new unique constraint for tenant + path
    # Note: This allows same path in different tenants
    # Old constraint (uq_virtual_path) will be dropped after backfill
    op.create_index(
        "uq_tenant_virtual_path",
        "file_paths",
        ["tenant_id", "virtual_path"],
        unique=True,
        postgresql_where=sa.text("tenant_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove tenant_id defense-in-depth (not recommended for production)."""
    op.drop_index("uq_tenant_virtual_path", table_name="file_paths")
    op.drop_index("idx_file_paths_tenant_path", table_name="file_paths")
    op.drop_index("idx_file_paths_tenant", table_name="file_paths")
    op.drop_column("file_paths", "tenant_id")
