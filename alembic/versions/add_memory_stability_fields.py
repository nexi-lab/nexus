"""feat(#1191): Add memory temporal stability classification fields

Revision ID: add_memory_stability_fields
Revises: add_ipc_messages_table
Create Date: 2026-02-14

Adds three nullable columns for auto-classification of memory temporal stability:
- temporal_stability (VARCHAR(20)): "static", "semi_dynamic", "dynamic", or NULL
- stability_confidence (FLOAT): 0.0-1.0 confidence score
- estimated_ttl_days (INTEGER): estimated time-to-live in days, NULL = infinite

Plus a B-tree index on temporal_stability for efficient filtering.

Backwards compatible: all columns are nullable, existing rows unaffected.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_memory_stability_fields"
down_revision: Union[str, Sequence[str], None] = "add_ipc_messages_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("memories", sa.Column("temporal_stability", sa.String(20), nullable=True))
    op.add_column("memories", sa.Column("stability_confidence", sa.Float(), nullable=True))
    op.add_column("memories", sa.Column("estimated_ttl_days", sa.Integer(), nullable=True))
    op.create_index("idx_memory_temporal_stability", "memories", ["temporal_stability"])


def downgrade() -> None:
    op.drop_index("idx_memory_temporal_stability", table_name="memories")
    op.drop_column("memories", "estimated_ttl_days")
    op.drop_column("memories", "stability_confidence")
    op.drop_column("memories", "temporal_stability")
