"""Add relationship extraction fields to memories (#1038)

Revision ID: add_relationship_extraction
Revises: add_temporal_metadata
Create Date: 2026-01-10

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_relationship_extraction"
down_revision: Union[str, Sequence[str], None] = "add_temporal_metadata"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add relationship extraction fields to memories table.

    Issue #1038: LLM-based relationship extraction at ingestion.
    - relationships_json: JSON array of extracted relationships (triplets)
    - relationship_count: Count of relationships for filtering
    """
    # Add relationship extraction columns
    op.add_column(
        "memories",
        sa.Column("relationships_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("relationship_count", sa.Integer(), nullable=True, default=0),
    )

    # Add index for filtering memories with relationships
    op.create_index("idx_memory_relationship_count", "memories", ["relationship_count"])


def downgrade() -> None:
    """Remove relationship extraction fields from memories table."""
    op.drop_index("idx_memory_relationship_count", table_name="memories")
    op.drop_column("memories", "relationship_count")
    op.drop_column("memories", "relationships_json")
