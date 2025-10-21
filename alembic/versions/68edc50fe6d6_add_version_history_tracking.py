"""add_version_history_tracking

Revision ID: 68edc50fe6d6
Revises: a277e3bdceb7
Create Date: 2025-10-21 11:11:58.659849

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "68edc50fe6d6"
down_revision: Union[str, Sequence[str], None] = "a277e3bdceb7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add current_version column to file_paths table
    op.add_column(
        "file_paths", sa.Column("current_version", sa.Integer(), nullable=False, server_default="1")
    )

    # Create version_history table
    op.create_table(
        "version_history",
        sa.Column("version_id", sa.String(length=36), nullable=False),
        sa.Column("resource_type", sa.String(length=50), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(length=255), nullable=True),
        sa.Column("parent_version_id", sa.String(length=36), nullable=True),
        sa.Column("source_type", sa.String(length=50), nullable=True),
        sa.Column("change_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("extra_metadata", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("version_id"),
        sa.ForeignKeyConstraint(
            ["parent_version_id"], ["version_history.version_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint("resource_type", "resource_id", "version_number", name="uq_version"),
    )

    # Create indexes
    op.create_index(
        "idx_version_history_resource", "version_history", ["resource_type", "resource_id"]
    )
    op.create_index("idx_version_history_content_hash", "version_history", ["content_hash"])
    op.create_index("idx_version_history_created_at", "version_history", ["created_at"])
    op.create_index("idx_version_history_parent", "version_history", ["parent_version_id"])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop indexes
    op.drop_index("idx_version_history_parent", "version_history")
    op.drop_index("idx_version_history_created_at", "version_history")
    op.drop_index("idx_version_history_content_hash", "version_history")
    op.drop_index("idx_version_history_resource", "version_history")

    # Drop version_history table
    op.drop_table("version_history")

    # Remove current_version column from file_paths
    op.drop_column("file_paths", "current_version")
