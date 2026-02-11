"""Add conflict_log table and conflict_strategy column (Issue #1130)

Revision ID: add_conflict_log
Revises: add_sync_backlog
Create Date: 2026-02-11

Creates the conflict_log table for audit trail of conflict
resolution events during bidirectional sync, and adds a
conflict_strategy column to mount_configs for per-mount
conflict strategy configuration.

Table: conflict_log
- Records every conflict detected, strategy applied, and outcome
- Supports manual conflict resolution via REST API
- 30-day TTL + 10K cap for bounded growth

Column: mount_configs.conflict_strategy
- Per-mount override for the global conflict resolution strategy
- NULL means "use global default"

References:
- https://github.com/nexi-lab/nexus/issues/1130
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from sqlalchemy import text

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_conflict_log"
down_revision: Union[str, Sequence[str], None] = "add_sync_backlog"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create conflict_log table + add conflict_strategy to mount_configs."""
    # --- conflict_log table ---
    op.create_table(
        "conflict_log",
        # Primary key
        sa.Column("id", sa.String(36), primary_key=True),
        # File identification
        sa.Column("path", sa.String(4096), nullable=False),
        sa.Column("backend_name", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="default"),
        # Resolution details
        sa.Column("strategy", sa.String(50), nullable=False),
        sa.Column("outcome", sa.String(50), nullable=False),
        # Nexus-side state
        sa.Column("nexus_content_hash", sa.String(64), nullable=True),
        sa.Column("nexus_mtime", sa.DateTime, nullable=True),
        sa.Column("nexus_size", sa.BigInteger, nullable=True),
        # Backend-side state
        sa.Column("backend_content_hash", sa.String(64), nullable=True),
        sa.Column("backend_mtime", sa.DateTime, nullable=True),
        sa.Column("backend_size", sa.BigInteger, nullable=True),
        # RENAME_CONFLICT copy path
        sa.Column("conflict_copy_path", sa.String(4096), nullable=True),
        # Status tracking
        sa.Column("status", sa.String(20), nullable=False, server_default="auto_resolved"),
        sa.Column("resolved_at", sa.DateTime, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    # Indexes for query patterns
    op.create_index("idx_cl_status_created", "conflict_log", ["status", "created_at"])
    op.create_index("idx_cl_backend_zone", "conflict_log", ["backend_name", "zone_id"])
    op.create_index("idx_cl_path", "conflict_log", ["path"])

    # BRIN index for time-range cleanup (PostgreSQL only)
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(
            text("CREATE INDEX idx_cl_created_brin ON conflict_log USING brin (created_at)")
        )

    # --- mount_configs.conflict_strategy column ---
    op.add_column(
        "mount_configs",
        sa.Column("conflict_strategy", sa.String(50), nullable=True),
    )


def downgrade() -> None:
    """Drop conflict_log table and conflict_strategy column."""
    # Drop column from mount_configs
    op.drop_column("mount_configs", "conflict_strategy")

    # Drop conflict_log table
    conn = op.get_bind()
    if conn.dialect.name == "postgresql":
        conn.execute(text("DROP INDEX IF EXISTS idx_cl_created_brin"))
    op.drop_index("idx_cl_path", table_name="conflict_log")
    op.drop_index("idx_cl_backend_zone", table_name="conflict_log")
    op.drop_index("idx_cl_status_created", table_name="conflict_log")
    op.drop_table("conflict_log")
