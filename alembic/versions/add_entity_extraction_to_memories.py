"""Add entity extraction fields to memories for SimpleMem symbolic layer (#1025)

Revision ID: add_entity_extraction
Revises: add_migration_history, make_tenant_id_non_nullable
Create Date: 2026-01-09

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_entity_extraction"
down_revision: Union[str, Sequence[str], None] = (
    "add_migration_history",
    "make_tenant_id_non_nullable",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add entity extraction fields to memories table.

    Issue #1025: SimpleMem symbolic layer for improved multi-hop queries.
    - entities_json: JSON array of extracted entities with positions
    - entity_types: Comma-separated entity types for quick filtering
    - person_refs: Comma-separated person names for quick person filtering
    """
    # Add entity extraction columns
    op.add_column(
        "memories",
        sa.Column("entities_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("entity_types", sa.String(255), nullable=True),
    )
    op.add_column(
        "memories",
        sa.Column("person_refs", sa.Text(), nullable=True),
    )

    # Add index on entity_types for efficient filtering
    op.create_index("idx_memory_entity_types", "memories", ["entity_types"])


def downgrade() -> None:
    """Remove entity extraction fields from memories table."""
    op.drop_index("idx_memory_entity_types", table_name="memories")
    op.drop_column("memories", "person_refs")
    op.drop_column("memories", "entity_types")
    op.drop_column("memories", "entities_json")
