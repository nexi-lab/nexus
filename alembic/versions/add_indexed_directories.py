"""feat: Add indexed_directories table and zones.indexing_mode column

Revision ID: idx_dirs_3698
Revises: a7ss_subj_iso
Create Date: 2026-04-09

Implements Issue #3698: Per-directory semantic index scoping API.

Changes:
1. Add ``indexing_mode`` column to ``zones`` (enum: 'all' | 'scoped'), default 'all'
   for backward compatibility. Existing zones keep current behavior (embed everything)
   until explicitly switched to 'scoped'.
2. Create ``indexed_directories`` table holding zone-scoped directory registrations.
   When a zone is in 'scoped' mode, only files under registered directory prefixes
   get embedded by the search daemon.

Design decisions (see Issue #3698 review):
- ``indexing_mode`` is a real column (not JSON inside zones.settings) because the
  bootstrap query needs to filter on it per-zone, and explicit > clever.
- ``indexed_directories`` has no ``recursive`` column: v1 is recursive-only; YAGNI.
- No ``created_by`` column: audit cross-cutting, not needed for v1.
- Unique constraint on (zone_id, directory_path) prevents duplicate registrations.

Uses batch_alter_table for SQLite compatibility.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "idx_dirs_3698"
down_revision: Union[str, Sequence[str], None] = "a7ss_subj_iso"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create indexed_directories and add zones.indexing_mode."""
    # 1. Add indexing_mode to zones table.
    #    server_default='all' ensures existing rows + inserts that omit the column
    #    continue to work unchanged (backward compat per Issue #3698 #2).
    with op.batch_alter_table("zones") as batch_op:
        batch_op.add_column(
            sa.Column(
                "indexing_mode",
                sa.String(16),
                nullable=False,
                server_default="all",
            )
        )

    # 2. Create indexed_directories table.
    op.create_table(
        "indexed_directories",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("directory_path", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "zone_id",
            "directory_path",
            name="uq_indexed_directories_zone_path",
        ),
    )

    # Index on zone_id so the bootstrap/filter queries can fetch a zone's
    # directories in O(log n). The unique constraint above doubles as a
    # composite index on (zone_id, directory_path), which is what the
    # filter helper actually needs, but add an explicit single-column
    # index to make "list all dirs for zone X" efficient as well.
    op.create_index(
        "idx_indexed_directories_zone",
        "indexed_directories",
        ["zone_id"],
    )


def downgrade() -> None:
    """Drop indexed_directories and remove zones.indexing_mode."""
    op.drop_index("idx_indexed_directories_zone", table_name="indexed_directories")
    op.drop_table("indexed_directories")

    with op.batch_alter_table("zones") as batch_op:
        batch_op.drop_column("indexing_mode")
