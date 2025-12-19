"""add text_pattern_ops index for LIKE queries

Revision ID: d5ed2f68c1bc
Revises: add_subscriptions_table
Create Date: 2025-12-19

Adds text_pattern_ops index for efficient LIKE prefix queries on virtual_path.
The existing btree indexes don't work well with LIKE 'prefix%' patterns.

PostgreSQL requires text_pattern_ops for prefix matching to use an index.
SQLite doesn't support this operator class, so we skip it there.

Performance impact:
- Before: Seq Scan on file_paths (159ms for 10k rows)
- After: Bitmap Index Scan (8ms for 10k rows)

Related queries in:
- src/nexus/storage/metadata_store.py (list, is_directory)
- src/nexus/search/vector_db.py (vector search with path filter)
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5ed2f68c1bc"
down_revision: Union[str, Sequence[str], None] = "add_subscriptions_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add text_pattern_ops index for LIKE prefix queries (PostgreSQL only)."""
    # Get the connection to check database type
    conn = op.get_bind()

    # Only create text_pattern_ops index on PostgreSQL
    # SQLite doesn't support operator classes
    if conn.dialect.name == "postgresql":
        # Use raw SQL because Alembic doesn't support postgresql_ops directly
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_file_paths_vpath_pattern
            ON file_paths (virtual_path text_pattern_ops)
        """)
        )

        # Also add for rebac_tuples subject_id and object_id for LIKE queries
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_rebac_subject_id_pattern
            ON rebac_tuples (subject_id text_pattern_ops)
        """)
        )
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_rebac_object_id_pattern
            ON rebac_tuples (object_id text_pattern_ops)
        """)
        )


def downgrade() -> None:
    """Remove text_pattern_ops indexes."""
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        op.drop_index("idx_rebac_object_id_pattern", table_name="rebac_tuples")
        op.drop_index("idx_rebac_subject_id_pattern", table_name="rebac_tuples")
        op.drop_index("idx_file_paths_vpath_pattern", table_name="file_paths")
