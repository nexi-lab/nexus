"""add_composite_indexes_for_performance

Revision ID: a1b2c3d4e5f6
Revises: 87a538c61ffa
Create Date: 2025-11-03 14:52:00.000000

Adds composite indexes for improved query performance:
- idx_tenant_path_prefix: Optimizes tenant-scoped prefix queries (WHERE tenant_id=X AND virtual_path LIKE 'prefix%')
- idx_content_hash_tenant: Optimizes CAS deduplication lookups by content hash and tenant

Related to GitHub issue #384
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "87a538c61ffa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add composite indexes for performance optimization."""
    # Note: idx_tenant_path_prefix is very similar to existing idx_file_paths_tenant_path
    # but we're adding it explicitly for clarity and potential future optimization
    # SQLite/PostgreSQL will handle duplicate index definitions gracefully

    # Composite index for tenant-scoped prefix queries
    # Optimizes queries like: SELECT * FROM file_paths WHERE tenant_id = ? AND virtual_path LIKE 'prefix%'
    op.create_index(
        "idx_tenant_path_prefix",
        "file_paths",
        ["tenant_id", "virtual_path"],
        unique=False,
    )

    # Composite index for CAS deduplication lookups
    # Optimizes queries like: SELECT * FROM file_paths WHERE content_hash = ? AND tenant_id = ?
    op.create_index(
        "idx_content_hash_tenant",
        "file_paths",
        ["content_hash", "tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    """Remove composite indexes."""
    op.drop_index("idx_content_hash_tenant", table_name="file_paths")
    op.drop_index("idx_tenant_path_prefix", table_name="file_paths")
