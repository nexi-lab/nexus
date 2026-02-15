"""feat(#1190): Add memory evolution relationship fields

Revision ID: add_memory_evolution_fields
Revises: add_memory_stability_fields
Create Date: 2026-02-15

Adds three nullable Text columns for memory evolution back-links:
- extends_ids: JSON array of memory IDs this memory extends
- extended_by_ids: JSON array of memory IDs that extend this memory
- derived_from_ids: JSON array of memory IDs this memory derives from

UPDATES relationships reuse existing supersedes_id/superseded_by_id columns.

Plus 3 B-tree indexes for efficient filtering.
Backwards compatible: all columns are nullable, existing rows unaffected.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_memory_evolution_fields"
down_revision: Union[str, Sequence[str], None] = "add_memory_stability_fields"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("extends_ids", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("extended_by_ids", sa.Text(), nullable=True))
    op.add_column("memories", sa.Column("derived_from_ids", sa.Text(), nullable=True))
    op.create_index("idx_memory_extends_ids", "memories", ["extends_ids"])
    op.create_index("idx_memory_extended_by_ids", "memories", ["extended_by_ids"])
    op.create_index("idx_memory_derived_from_ids", "memories", ["derived_from_ids"])


def downgrade() -> None:
    op.drop_index("idx_memory_derived_from_ids", table_name="memories")
    op.drop_index("idx_memory_extended_by_ids", table_name="memories")
    op.drop_index("idx_memory_extends_ids", table_name="memories")
    op.drop_column("memories", "derived_from_ids")
    op.drop_column("memories", "extended_by_ids")
    op.drop_column("memories", "extends_ids")
