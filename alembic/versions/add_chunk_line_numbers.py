"""add_chunk_line_numbers

Add line_start and line_end columns to document_chunks table.
These columns store the source line numbers for each chunk,
enabling precise source location in search results.

Revision ID: add_chunk_line_numbers
Revises: b02814593b71
Create Date: 2025-12-20

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_chunk_line_numbers"
down_revision: Union[str, Sequence[str], None] = "b02814593b71"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add line_start and line_end columns to document_chunks."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.add_column(sa.Column("line_start", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("line_end", sa.Integer(), nullable=True))


def downgrade() -> None:
    """Remove line_start and line_end columns from document_chunks."""
    with op.batch_alter_table("document_chunks", schema=None) as batch_op:
        batch_op.drop_column("line_end")
        batch_op.drop_column("line_start")
