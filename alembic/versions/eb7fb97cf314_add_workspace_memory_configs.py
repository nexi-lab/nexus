"""add_workspace_memory_configs

This migration adds tables for workspace and memory registry configuration.

Workspaces are directories that support:
- Snapshots (point-in-time captures)
- Versioning (rollback)
- Workspace logs

Memories are directories that support:
- Memory consolidation
- Semantic search
- Memory versioning

Revision ID: eb7fb97cf314
Revises: p0_api_key_cols
Create Date: 2025-10-25 01:16:06.723920

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "eb7fb97cf314"
down_revision: Union[str, Sequence[str], None] = "p0_api_key_cols"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add workspace_configs and memory_configs tables."""

    # Create workspace_configs table
    op.create_table(
        "workspace_configs",
        sa.Column("path", sa.Text(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),  # JSON as text for SQLite compat
        sa.PrimaryKeyConstraint("path"),
    )

    # Create indexes for workspace_configs
    op.create_index("idx_workspace_configs_created_at", "workspace_configs", ["created_at"])

    # Create memory_configs table
    op.create_table(
        "memory_configs",
        sa.Column("path", sa.Text(), nullable=False, primary_key=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("created_by", sa.String(255), nullable=True),
        sa.Column("metadata", sa.Text(), nullable=True),  # JSON as text for SQLite compat
        sa.PrimaryKeyConstraint("path"),
    )

    # Create indexes for memory_configs
    op.create_index("idx_memory_configs_created_at", "memory_configs", ["created_at"])


def downgrade() -> None:
    """Remove workspace_configs and memory_configs tables."""

    # Drop indexes
    op.drop_index("idx_memory_configs_created_at", table_name="memory_configs")
    op.drop_index("idx_workspace_configs_created_at", table_name="workspace_configs")

    # Drop tables
    op.drop_table("memory_configs")
    op.drop_table("workspace_configs")
