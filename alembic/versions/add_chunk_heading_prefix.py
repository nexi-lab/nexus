"""add_chunk_heading_prefix

Add heading_prefix column to document_chunks table.
Stores the nearest ancestor heading text for each chunk,
enabling heading-aware macro-chunk expansion in search results.

Revision ID: add_chunk_heading_prefix
Revises: merge_us_pay_amounts
Create Date: 2026-06-17

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_chunk_heading_prefix"
down_revision: Union[str, Sequence[str], None] = "merge_us_pay_amounts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add heading_prefix column to document_chunks."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("heading_prefix", sa.Text(), nullable=True))


def downgrade() -> None:
    """Remove heading_prefix column from document_chunks."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.drop_column("heading_prefix")
