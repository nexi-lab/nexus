"""Add document_skeleton table for global path+title index (Issue #3725).

Revision ID: add_document_skeleton
Revises: idx_dirs_3698
Create Date: 2026-04-11

Adds the document_skeleton table — a lightweight, globally-indexed record for
every file in every zone. No embeddings, no LLM. Feeds the /api/v2/search/locate
endpoint via BM25S with per-field column weighting (path vs title).

Design notes:
    - path_tokens column intentionally absent: virtual_path and title are passed
      as separate BM25S fields at index time, not stored pre-tokenized.
    - skeleton_content_hash = sha256(first 2KB): skip guard in SkeletonPipeConsumer.
    - ON DELETE CASCADE from file_paths handles sync automatically.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_document_skeleton"
down_revision: Union[str, Sequence[str], None] = "idx_dirs_3698"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create document_skeleton table."""
    op.create_table(
        "document_skeleton",
        sa.Column("path_id", sa.String(36), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False, server_default="root"),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("skeleton_content_hash", sa.String(64), nullable=True),
        sa.Column("indexed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["path_id"],
            ["file_paths.path_id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("path_id"),
    )
    op.create_index(
        "idx_document_skeleton_zone",
        "document_skeleton",
        ["zone_id"],
    )
    op.create_index(
        "idx_document_skeleton_zone_path_title",
        "document_skeleton",
        ["zone_id", "path_id", "title"],
    )


def downgrade() -> None:
    """Drop document_skeleton table."""
    op.drop_index("idx_document_skeleton_zone_path_title", table_name="document_skeleton")
    op.drop_index("idx_document_skeleton_zone", table_name="document_skeleton")
    op.drop_table("document_skeleton")
