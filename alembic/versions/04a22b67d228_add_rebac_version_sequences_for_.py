"""add_rebac_version_sequences_for_consistency_tokens

Revision ID: 04a22b67d228
Revises: da7be77fac7c
Create Date: 2025-10-25 11:36:47.219462

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "04a22b67d228"
down_revision: Union[str, Sequence[str], None] = "da7be77fac7c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add per-tenant version sequence table for ReBAC consistency tokens.
    This replaces the in-memory counter with a monotonic DB-backed sequence.
    """
    # Create table to store per-tenant version counters
    op.create_table(
        "rebac_version_sequences",
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("current_version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id"),
    )

    # Create index for fast lookups
    op.create_index(
        "ix_rebac_version_sequences_tenant_id", "rebac_version_sequences", ["tenant_id"]
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_rebac_version_sequences_tenant_id", table_name="rebac_version_sequences")
    op.drop_table("rebac_version_sequences")
