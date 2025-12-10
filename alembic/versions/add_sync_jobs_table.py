"""Add sync_jobs table for async sync operations (Issue #609)

Revision ID: add_sync_jobs
Revises: add_rebac_indexes
Create Date: 2025-12-10

Adds sync_jobs table to track async sync_mount operations.
Supports progress monitoring, cancellation, and job history.

Features:
- Progress tracking (0-100% with detailed info)
- Job status (pending, running, completed, failed, cancelled)
- Sync parameters stored for reference
- Final results stored on completion
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_sync_jobs"
down_revision: Union[str, Sequence[str], None] = "add_rebac_indexes"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add sync_jobs table."""
    op.create_table(
        "sync_jobs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("mount_point", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("progress_detail", sa.Text(), nullable=True),  # JSON
        sa.Column("sync_params", sa.Text(), nullable=True),  # JSON
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),  # JSON
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # Create indexes
    op.create_index("idx_sync_jobs_mount_point", "sync_jobs", ["mount_point"])
    op.create_index("idx_sync_jobs_status", "sync_jobs", ["status"])
    op.create_index("idx_sync_jobs_created_at", "sync_jobs", ["created_at"])
    op.create_index("idx_sync_jobs_created_by", "sync_jobs", ["created_by"])


def downgrade() -> None:
    """Remove sync_jobs table."""
    op.drop_index("idx_sync_jobs_created_by", table_name="sync_jobs")
    op.drop_index("idx_sync_jobs_created_at", table_name="sync_jobs")
    op.drop_index("idx_sync_jobs_status", table_name="sync_jobs")
    op.drop_index("idx_sync_jobs_mount_point", table_name="sync_jobs")
    op.drop_table("sync_jobs")
