"""Add memory hierarchy fields for SimpleMem recursive consolidation (#1029)

Revision ID: add_memory_hierarchy_fields
Revises: add_graph_storage_tables
Create Date: 2026-01-13

Adds fields for hierarchical memory abstraction:
- abstraction_level: Level in hierarchy (0=atomic, 1=cluster, 2=abstract, etc.)
- parent_memory_id: Points to higher-level abstraction
- child_memory_ids: JSON array of lower-level memory IDs
- is_archived: True if consolidated into higher level

This enables:
- Multi-level memory hierarchy (atoms → clusters → abstracts)
- Hierarchy-aware retrieval (prefer abstracts, expand to children)
- Progressive consolidation without losing granular memories

Issue #1029: Hierarchical memory abstraction
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_memory_hierarchy_fields"
down_revision: Union[str, Sequence[str], None] = "add_graph_storage_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add hierarchy fields to memories table."""
    # Add abstraction_level column (0 = atomic, 1 = cluster, 2 = abstract, etc.)
    op.add_column(
        "memories",
        sa.Column(
            "abstraction_level",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Add parent_memory_id column (points to higher-level abstraction)
    op.add_column(
        "memories",
        sa.Column(
            "parent_memory_id",
            sa.String(36),
            nullable=True,
        ),
    )

    # Add child_memory_ids column (JSON array of lower-level memory IDs)
    op.add_column(
        "memories",
        sa.Column(
            "child_memory_ids",
            sa.Text(),
            nullable=True,
        ),
    )

    # Add is_archived column (True if consolidated into higher level)
    op.add_column(
        "memories",
        sa.Column(
            "is_archived",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )

    # Create indexes for efficient hierarchy queries
    op.create_index(
        "idx_memory_abstraction_level",
        "memories",
        ["abstraction_level"],
    )
    op.create_index(
        "idx_memory_parent",
        "memories",
        ["parent_memory_id"],
    )
    op.create_index(
        "idx_memory_archived",
        "memories",
        ["is_archived"],
    )


def downgrade() -> None:
    """Remove hierarchy fields from memories table."""
    # Drop indexes
    op.drop_index("idx_memory_archived", table_name="memories")
    op.drop_index("idx_memory_parent", table_name="memories")
    op.drop_index("idx_memory_abstraction_level", table_name="memories")

    # Drop columns
    op.drop_column("memories", "is_archived")
    op.drop_column("memories", "child_memory_ids")
    op.drop_column("memories", "parent_memory_id")
    op.drop_column("memories", "abstraction_level")
