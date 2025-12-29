"""Add pg_trgm GIN index for fast substring pattern matching

Revision ID: add_pg_trgm_index
Revises: update_file_namespace_shared
Create Date: 2025-12-28

Adds pg_trgm extension and GIN trigram index for efficient LIKE '%pattern%' queries.
The existing text_pattern_ops index only works for prefix patterns (LIKE 'prefix%').

pg_trgm enables substring matching by indexing 3-character trigrams:
- Path: "src/components/Button.tsx"
- Trigrams: "src", "rc/", "c/c", "/co", "com", ...
- Query: LIKE '%Button%' -> Fast index lookup instead of full scan

Performance impact (estimated on 100K files):
- Before: Seq Scan (~500ms for LIKE '%pattern%')
- After: Bitmap Index Scan (~5-10ms)

Requirements:
- PostgreSQL 9.1+ (pg_trgm is a contrib module)
- Pattern must be >= 3 characters for index use

References:
- https://www.postgresql.org/docs/current/pgtrgm.html
- Issue #946: perf(db): Add pg_trgm indexes for fast glob/LIKE pattern matching
"""

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision: str = "add_pg_trgm_index"
down_revision: Union[str, Sequence[str], None] = "update_file_namespace_shared"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add pg_trgm extension and GIN trigram index (PostgreSQL only)."""
    conn = op.get_bind()

    # Only create pg_trgm index on PostgreSQL
    # SQLite doesn't support this extension
    if conn.dialect.name == "postgresql":
        # Enable pg_trgm extension
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))

        # Add GIN trigram index on virtual_path for substring matching
        conn.execute(
            text("""
            CREATE INDEX IF NOT EXISTS idx_file_paths_vpath_trgm
            ON file_paths USING GIN (virtual_path gin_trgm_ops)
        """)
        )


def downgrade() -> None:
    """Remove pg_trgm index (extension left in place as other things may use it)."""
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        op.drop_index("idx_file_paths_vpath_trgm", table_name="file_paths")
