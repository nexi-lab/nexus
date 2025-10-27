"""P0-2: Add tenant_id to rebac_tuples for tenant isolation

Revision ID: 4f0aaaec2735
Revises: 68edc50fe6d6
Create Date: 2025-10-24 23:10:04.100622

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4f0aaaec2735"
down_revision: Union[str, Sequence[str], None] = "68edc50fe6d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema - Add tenant_id columns for tenant isolation (P0-2)."""

    # Add tenant_id columns to rebac_tuples (nullable initially)
    op.add_column("rebac_tuples", sa.Column("tenant_id", sa.String(255), nullable=True))
    op.add_column("rebac_tuples", sa.Column("subject_tenant_id", sa.String(255), nullable=True))
    op.add_column("rebac_tuples", sa.Column("object_tenant_id", sa.String(255), nullable=True))

    # Add tenant_id column to rebac_check_cache (nullable)
    op.add_column("rebac_check_cache", sa.Column("tenant_id", sa.String(255), nullable=True))

    # Create tenant-scoped indexes for rebac_tuples
    op.create_index(
        "idx_rebac_tenant_subject", "rebac_tuples", ["tenant_id", "subject_type", "subject_id"]
    )
    op.create_index(
        "idx_rebac_tenant_object", "rebac_tuples", ["tenant_id", "object_type", "object_id"]
    )

    # Create tenant-scoped index for cache
    op.create_index(
        "idx_rebac_cache_tenant_check",
        "rebac_check_cache",
        ["tenant_id", "subject_type", "subject_id", "permission", "object_type", "object_id"],
    )

    # Note: Columns left nullable for backward compatibility
    # Production deployments should run backfill and make NOT NULL


def downgrade() -> None:
    """Downgrade schema - Remove tenant_id columns."""

    # Drop indexes
    op.drop_index("idx_rebac_cache_tenant_check", table_name="rebac_check_cache")
    op.drop_index("idx_rebac_tenant_object", table_name="rebac_tuples")
    op.drop_index("idx_rebac_tenant_subject", table_name="rebac_tuples")

    # Drop columns
    op.drop_column("rebac_check_cache", "tenant_id")
    op.drop_column("rebac_tuples", "object_tenant_id")
    op.drop_column("rebac_tuples", "subject_tenant_id")
    op.drop_column("rebac_tuples", "tenant_id")
