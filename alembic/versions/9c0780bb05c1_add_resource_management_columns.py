"""add_resource_management_columns

Revision ID: 9c0780bb05c1
Revises: 58d58578fce0
Create Date: 2025-10-17 00:45:39.978492

Adds columns for resource management and cache eviction (Issue #36):
- file_paths.accessed_at: Track last access time for cache eviction
- file_paths.locked_by: Track file locks for concurrent access control
- content_chunks.protected_until: Grace period before garbage collection
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9c0780bb05c1"
down_revision: Union[str, Sequence[str], None] = "58d58578fce0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add resource management columns."""
    # Add columns to file_paths table
    op.add_column("file_paths", sa.Column("accessed_at", sa.DateTime(), nullable=True))
    op.add_column("file_paths", sa.Column("locked_by", sa.String(length=255), nullable=True))

    # Add column to content_chunks table
    op.add_column("content_chunks", sa.Column("protected_until", sa.DateTime(), nullable=True))

    # Add indexes for query performance
    op.create_index("idx_file_paths_accessed_at", "file_paths", ["accessed_at"])
    op.create_index("idx_file_paths_locked_by", "file_paths", ["locked_by"])
    op.create_index("idx_content_chunks_last_accessed", "content_chunks", ["last_accessed_at"])


def downgrade() -> None:
    """Remove resource management columns."""
    # Drop indexes
    op.drop_index("idx_content_chunks_last_accessed", table_name="content_chunks")
    op.drop_index("idx_file_paths_locked_by", table_name="file_paths")
    op.drop_index("idx_file_paths_accessed_at", table_name="file_paths")

    # Drop columns
    op.drop_column("content_chunks", "protected_until")
    op.drop_column("file_paths", "locked_by")
    op.drop_column("file_paths", "accessed_at")
