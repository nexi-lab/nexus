"""add_indexed_content_hash_and_last_indexed_at

Revision ID: 0dec0068b76e
Revises: add_chunk_line_numbers
Create Date: 2025-12-22 01:28:49.051379

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0dec0068b76e"
down_revision: Union[str, Sequence[str], None] = "add_chunk_line_numbers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add indexed_content_hash and last_indexed_at columns to file_paths.

    These columns track semantic search indexing status:
    - indexed_content_hash: Hash of content when last indexed (skip re-indexing if unchanged)
    - last_indexed_at: Timestamp of last successful indexing
    """
    op.add_column("file_paths", sa.Column("indexed_content_hash", sa.String(64), nullable=True))
    op.add_column("file_paths", sa.Column("last_indexed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Remove indexed_content_hash and last_indexed_at columns."""
    op.drop_column("file_paths", "last_indexed_at")
    op.drop_column("file_paths", "indexed_content_hash")
