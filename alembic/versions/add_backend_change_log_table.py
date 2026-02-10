"""Add backend_change_log table for delta sync tracking (Issue #1127)

Revision ID: add_backend_change_log
Revises: add_migration_history
Create Date: 2026-02-10

Creates the backend_change_log table used by ChangeLogStore to track
the last synced state of each file per backend, enabling incremental
sync by comparing against current backend state.

Indexes:
- uq_backend_change_log: Composite unique (path, backend_name, zone_id)
- idx_bcl_path_backend: Lookup by path + backend
- idx_bcl_synced_at: Range scans by backend + time
- idx_bcl_zone: Zone isolation queries
- idx_bcl_synced_brin: BRIN index for time-series (PostgreSQL only)

References:
- https://github.com/nexi-lab/nexus/issues/1127
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_backend_change_log"
down_revision: Union[str, Sequence[str], None] = "add_migration_history"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create backend_change_log table with indexes."""
    op.create_table(
        "backend_change_log",
        # Primary key
        sa.Column("id", sa.String(36), primary_key=True),
        # File identification (composite unique key)
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("backend_name", sa.String(255), nullable=False),
        # Change detection fields (rsync-inspired quick check)
        sa.Column("size_bytes", sa.BigInteger, nullable=True),
        sa.Column("mtime", sa.DateTime, nullable=True),
        # Backend-specific version tracking
        sa.Column("backend_version", sa.String(255), nullable=True),
        # Content hash fallback
        sa.Column("content_hash", sa.String(64), nullable=True),
        # Sync tracking
        sa.Column("synced_at", sa.DateTime, nullable=False),
        # Zone isolation
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="default"),
        # Unique constraint
        sa.UniqueConstraint("path", "backend_name", "zone_id", name="uq_backend_change_log"),
    )

    # Lookup indexes
    op.create_index("idx_bcl_path_backend", "backend_change_log", ["path", "backend_name"])
    op.create_index("idx_bcl_synced_at", "backend_change_log", ["backend_name", "synced_at"])
    op.create_index("idx_bcl_zone", "backend_change_log", ["zone_id"])

    # BRIN index for time-series queries (PostgreSQL only)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            text("CREATE INDEX idx_bcl_synced_brin ON backend_change_log USING brin (synced_at)")
        )


def downgrade() -> None:
    """Drop backend_change_log table and its indexes."""
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(text("DROP INDEX IF EXISTS idx_bcl_synced_brin"))
    op.drop_index("idx_bcl_zone", table_name="backend_change_log")
    op.drop_index("idx_bcl_synced_at", table_name="backend_change_log")
    op.drop_index("idx_bcl_path_backend", table_name="backend_change_log")
    op.drop_table("backend_change_log")
