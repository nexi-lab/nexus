"""Add sync_backlog table for bidirectional sync (Issue #1129)

Revision ID: add_sync_backlog
Revises: add_backend_change_log
Create Date: 2026-02-11

Creates the sync_backlog table used by SyncBacklogStore to track
pending write-back operations from Nexus to source backends.

Features:
- Upsert coalescing via unique constraint on (path, backend_name, zone_id, status)
- Status-based processing: pending -> in_progress -> completed/failed/expired
- Retry tracking with configurable max_retries
- TTL-based and cap-based expiry for bounded growth

Indexes:
- uq_sync_backlog_pending: Unique (path, backend_name, zone_id, status) for coalescing
- idx_sb_status_created: Pending fetch ordered by creation time
- idx_sb_backend_zone_status: Per-backend processing
- idx_sb_created_brin: BRIN index for time-range cleanup (PostgreSQL only)

References:
- https://github.com/nexi-lab/nexus/issues/1129
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_sync_backlog"
down_revision: Union[str, Sequence[str], None] = "add_backend_change_log"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create sync_backlog table with indexes."""
    op.create_table(
        "sync_backlog",
        # Primary key
        sa.Column("id", sa.String(36), primary_key=True),
        # File identification
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("backend_name", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="default"),
        # Operation details
        sa.Column("operation_type", sa.String(50), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("new_path", sa.String(4096), nullable=True),
        # Status tracking
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="5"),
        # Timestamps
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.Column("last_attempted_at", sa.DateTime, nullable=True),
        # Error tracking
        sa.Column("error_message", sa.Text, nullable=True),
        # Unique constraint for upsert coalescing
        sa.UniqueConstraint(
            "path", "backend_name", "zone_id", "status", name="uq_sync_backlog_pending"
        ),
    )

    # Indexes for query patterns
    op.create_index("idx_sb_status_created", "sync_backlog", ["status", "created_at"])
    op.create_index(
        "idx_sb_backend_zone_status", "sync_backlog", ["backend_name", "zone_id", "status"]
    )

    # BRIN index for time-range cleanup (PostgreSQL only)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            text("CREATE INDEX idx_sb_created_brin ON sync_backlog USING brin (created_at)")
        )


def downgrade() -> None:
    """Drop sync_backlog table and its indexes."""
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(text("DROP INDEX IF EXISTS idx_sb_created_brin"))
    op.drop_index("idx_sb_backend_zone_status", table_name="sync_backlog")
    op.drop_index("idx_sb_status_created", table_name="sync_backlog")
    op.drop_table("sync_backlog")
