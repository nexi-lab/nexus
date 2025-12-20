"""content_cache use bytea instead of base64 text

Revision ID: content_cache_use_bytea
Revises: d5ed2f68c1bc
Create Date: 2025-12-19

Changes content_cache.content_binary from TEXT (base64 encoded) to BYTEA (native binary).

Benefits:
- 33% storage reduction (no base64 overhead)
- No encode/decode CPU overhead
- Faster reads/writes

Since content_cache is ephemeral (can be regenerated via sync), we truncate the table
during migration rather than attempting complex base64 decoding.

Related to: Issue #716
"""

from collections.abc import Sequence
from typing import Union

from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "content_cache_use_bytea"
down_revision: Union[str, Sequence[str], None] = "d5ed2f68c1bc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Change content_binary from TEXT to BYTEA (PostgreSQL) or BLOB (SQLite)."""
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        # Truncate cache table - data can be regenerated via sync
        # This avoids complex base64 decoding during migration
        conn.execute(text("TRUNCATE TABLE content_cache"))

        # Change column type from TEXT to BYTEA
        conn.execute(
            text("""
            ALTER TABLE content_cache
            ALTER COLUMN content_binary TYPE bytea
            USING NULL
        """)
        )
    elif conn.dialect.name == "sqlite":
        # SQLite is flexible with types, but we should clear the data
        # since the old data is base64-encoded strings
        conn.execute(text("DELETE FROM content_cache"))
        # SQLite doesn't require ALTER COLUMN for type changes
        # The column will accept bytes directly


def downgrade() -> None:
    """Revert content_binary from BYTEA to TEXT."""
    conn = op.get_bind()

    if conn.dialect.name == "postgresql":
        # Truncate and revert type
        conn.execute(text("TRUNCATE TABLE content_cache"))
        conn.execute(
            text("""
            ALTER TABLE content_cache
            ALTER COLUMN content_binary TYPE text
            USING NULL
        """)
        )
    elif conn.dialect.name == "sqlite":
        # Clear cache data
        conn.execute(text("DELETE FROM content_cache"))
