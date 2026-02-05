"""Add bi-temporal validity fields to memories (#1183)

Issue #1183: Bi-temporal fields for MemoryModel

Adds valid_at and invalid_at columns for bi-temporal fact tracking:
- valid_at: When the fact became true in the real world (NULL = use created_at)
- invalid_at: When the fact became false (NULL = still valid)

This enables:
- Point-in-time queries ("What did we know as of date X?")
- Current fact filtering ("Only show currently valid facts")
- Fact invalidation without deletion (soft temporal delete)
- GDPR/HIPAA compliance with temporal audit trails

Revision ID: add_bitemporal_validity
Revises: add_filesystem_version_sequences
Create Date: 2026-02-05 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_bitemporal_validity"
down_revision: Union[str, Sequence[str], None] = "add_filesystem_version_sequences"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add bi-temporal validity fields to memories table.

    Adds:
    - valid_at: When fact became valid in real world (NULL defaults to created_at)
    - invalid_at: When fact became invalid (NULL = still valid)
    - B-tree indexes for point queries and NULL filtering
    - BRIN index for PostgreSQL time-range scans
    """
    # Add valid_at column (when fact became valid in real world)
    op.add_column(
        "memories",
        sa.Column("valid_at", sa.DateTime(), nullable=True),
    )

    # Add invalid_at column (when fact became invalid, NULL = still valid)
    op.add_column(
        "memories",
        sa.Column("invalid_at", sa.DateTime(), nullable=True),
    )

    # B-tree indexes for point queries and NULL filtering
    op.create_index("idx_memory_valid_at", "memories", ["valid_at"])
    op.create_index("idx_memory_invalid_at", "memories", ["invalid_at"])

    # BRIN index for PostgreSQL time-range scans (efficient for ordered data)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.create_index(
            "idx_memory_valid_at_brin",
            "memories",
            ["valid_at"],
            postgresql_using="brin",
        )


def downgrade() -> None:
    """Remove bi-temporal validity fields from memories table."""
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_index("idx_memory_valid_at_brin", table_name="memories")

    op.drop_index("idx_memory_invalid_at", table_name="memories")
    op.drop_index("idx_memory_valid_at", table_name="memories")
    op.drop_column("memories", "invalid_at")
    op.drop_column("memories", "valid_at")
