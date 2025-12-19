"""Add backend_version index for cache invalidation queries

Revision ID: add_backend_version_idx
Revises: d5ed2f68c1bc
Create Date: 2025-12-19

Adds partial index on backend_version column in content_cache table
to speed up cache invalidation queries.

Related to: #703 (PostgreSQL L2 Cache Performance)
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_backend_version_idx"
down_revision: Union[str, Sequence[str], None] = "d5ed2f68c1bc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add backend_version index for cache invalidation queries."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # Partial index: only index non-null backend_version values
        # This is more efficient for invalidation queries
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_content_cache_backend_version
            ON content_cache (backend_version)
            WHERE backend_version IS NOT NULL
            """
        )
    else:
        # SQLite: regular index (partial indexes have limited support)
        op.create_index(
            "idx_content_cache_backend_version",
            "content_cache",
            ["backend_version"],
        )


def downgrade() -> None:
    """Remove backend_version index."""
    op.drop_index("idx_content_cache_backend_version", table_name="content_cache")
