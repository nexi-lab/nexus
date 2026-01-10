"""Add temporal metadata fields to memories for date-based queries (#1028)

Revision ID: add_temporal_metadata
Revises: add_entity_extraction
Create Date: 2026-01-10

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_temporal_metadata"
down_revision: Union[str, Sequence[str], None] = "add_entity_extraction"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add temporal metadata fields to memories table.

    Issue #1028: Temporal anchoring for relative date expressions.
    - temporal_refs_json: JSON array of extracted temporal references
    - earliest_date: Earliest date mentioned (indexed for queries)
    - latest_date: Latest date mentioned (indexed for queries)
    """
    # Add temporal metadata columns
    op.add_column(
        "memories",
        sa.Column("temporal_refs_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("earliest_date", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("latest_date", sa.DateTime(), nullable=True),
    )

    # Add indexes for efficient date-range queries
    op.create_index("idx_memory_earliest_date", "memories", ["earliest_date"])
    op.create_index("idx_memory_latest_date", "memories", ["latest_date"])


def downgrade() -> None:
    """Remove temporal metadata fields from memories table."""
    op.drop_index("idx_memory_latest_date", table_name="memories")
    op.drop_index("idx_memory_earliest_date", table_name="memories")
    op.drop_column("memories", "latest_date")
    op.drop_column("memories", "earliest_date")
    op.drop_column("memories", "temporal_refs_json")
