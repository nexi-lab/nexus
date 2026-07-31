"""Add weight column to path_contexts (Issue #4544).

Nullable, no server default: NULL ≡ 1.0 in application code, so existing
rows and weightless new rows rank exactly as before the migration.

Revision ID: add_path_context_weight
Revises: align_graph_zone_columns
Create Date: 2026-07-31
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_path_context_weight"
down_revision: Union[str, Sequence[str], None] = "align_graph_zone_columns"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add nullable weight column."""
    op.add_column("path_contexts", sa.Column("weight", sa.Float(), nullable=True))


def downgrade() -> None:
    """Drop weight column."""
    op.drop_column("path_contexts", "weight")
