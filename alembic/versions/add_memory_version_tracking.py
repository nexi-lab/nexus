"""Add current_version field to memories for version tracking (#1184)

Revision ID: add_memory_version_tracking
Revises: add_nexus_pay_models
Create Date: 2026-02-05

Adds version tracking to memories to connect with VersionHistoryModel:
- current_version: Tracks current version number (starts at 1)

This enables:
- Full audit trail for memory changes
- Rollback to previous versions
- Version comparison/diff
- Same versioning infrastructure as files

Issue #1184: Wire memory versions to VersionHistoryModel
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_memory_version_tracking"
down_revision: Union[str, Sequence[str], None] = "add_nexus_pay_models"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add current_version column to memories table."""
    # Add current_version column (defaults to 1 for new memories)
    op.add_column(
        "memories",
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )

    # Create index for version queries
    op.create_index(
        "idx_memory_current_version",
        "memories",
        ["current_version"],
    )


def downgrade() -> None:
    """Remove current_version column from memories table."""
    # Drop index
    op.drop_index("idx_memory_current_version", table_name="memories")

    # Drop column
    op.drop_column("memories", "current_version")
