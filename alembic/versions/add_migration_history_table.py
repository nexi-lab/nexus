"""Add migration_history table for tracking migrations

Revision ID: add_migration_history
Revises: tune_hnsw_index_for_100k_vectors
Create Date: 2025-01-08

Adds migration_history table for tracking version upgrades, rollbacks,
and import operations. This enables:
- Audit trail of all migration operations
- Rollback point identification
- Migration status tracking

Issue #165: Migration Tools & Upgrade Paths
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_migration_history"
down_revision: Union[str, Sequence[str], None] = "tune_hnsw_index_for_100k_vectors"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create migration_history table."""
    op.create_table(
        "migration_history",
        # Primary key
        sa.Column("id", sa.String(36), primary_key=True),
        # Version information
        sa.Column("from_version", sa.String(20), nullable=False),
        sa.Column("to_version", sa.String(20), nullable=False),
        # Migration type: 'upgrade', 'rollback', 'import'
        sa.Column("migration_type", sa.String(50), nullable=False),
        # Status: 'pending', 'running', 'completed', 'failed', 'rolled_back'
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        # Backup information
        sa.Column("backup_path", sa.Text(), nullable=True),
        # Timestamps
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        # Error tracking
        sa.Column("error_message", sa.Text(), nullable=True),
        # Additional metadata as JSON
        sa.Column("metadata_json", sa.Text(), nullable=True),
    )

    # Create indexes
    op.create_index(
        "idx_migration_history_status",
        "migration_history",
        ["status"],
    )
    op.create_index(
        "idx_migration_history_started_at",
        "migration_history",
        ["started_at"],
    )


def downgrade() -> None:
    """Drop migration_history table."""
    # Drop indexes
    op.drop_index("idx_migration_history_started_at", table_name="migration_history")
    op.drop_index("idx_migration_history_status", table_name="migration_history")

    # Drop table
    op.drop_table("migration_history")
