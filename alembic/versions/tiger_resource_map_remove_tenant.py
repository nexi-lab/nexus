"""Remove tenant_id from Tiger Resource Map

Revision ID: tiger_resource_map_remove_tenant
Revises: tiger_cache_remove_tenant
Create Date: 2026-01-01

Issue #979: Cross-tenant resource map optimization

This migration removes tenant_id from the tiger_resource_map table.
Resource paths are globally unique (e.g., /skills/system/docs is the same
file regardless of which tenant queries it). Tenant isolation is enforced
at the bitmap/permission level, not the resource ID mapping.

Changes:
- Drop old unique constraint (resource_type, resource_id, tenant_id)
- Create new unique constraint (resource_type, resource_id)
- Update index to remove tenant_id
- Drop tenant_id column entirely

This fixes the cross-tenant resource map lookup issue where /skills/system/*
paths stored in 'default' tenant couldn't be found when queried by users
in other tenants.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "tiger_resource_map_remove_tenant"
down_revision: Union[str, Sequence[str], None] = "tiger_cache_remove_tenant"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove tenant_id from Tiger Resource Map."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # Step 1: Delete duplicate rows that would conflict after removing tenant_id
        # Keep only the row with the lowest resource_int_id (oldest) for each (type, id) pair
        bind.execute(
            sa.text("""
            DELETE FROM tiger_resource_map t1
            USING tiger_resource_map t2
            WHERE t1.resource_int_id > t2.resource_int_id
              AND t1.resource_type = t2.resource_type
              AND t1.resource_id = t2.resource_id
        """)
        )

        # Step 2: Drop old unique constraint
        op.drop_constraint("uq_tiger_resource", "tiger_resource_map", type_="unique")

        # Step 3: Drop old index
        op.drop_index("idx_tiger_resource_lookup", table_name="tiger_resource_map")

        # Step 4: Drop tenant_id column
        op.drop_column("tiger_resource_map", "tenant_id")

        # Step 5: Create new unique constraint without tenant_id
        op.create_unique_constraint(
            "uq_tiger_resource",
            "tiger_resource_map",
            ["resource_type", "resource_id"],
        )

        # Step 6: Create new index without tenant_id
        op.create_index(
            "idx_tiger_resource_lookup",
            "tiger_resource_map",
            ["resource_type", "resource_id"],
        )
    else:
        # SQLite: Need to recreate table
        with op.batch_alter_table("tiger_resource_map") as batch_op:
            batch_op.drop_index("idx_tiger_resource_lookup")
            batch_op.drop_constraint("uq_tiger_resource", type_="unique")
            batch_op.drop_column("tenant_id")
            batch_op.create_unique_constraint(
                "uq_tiger_resource",
                ["resource_type", "resource_id"],
            )
            batch_op.create_index(
                "idx_tiger_resource_lookup",
                ["resource_type", "resource_id"],
            )


def downgrade() -> None:
    """Restore tenant_id to Tiger Resource Map."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # Add tenant_id column back (with default value)
        op.add_column(
            "tiger_resource_map",
            sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default"),
        )

        # Drop new constraint and index
        op.drop_constraint("uq_tiger_resource", "tiger_resource_map", type_="unique")
        op.drop_index("idx_tiger_resource_lookup", table_name="tiger_resource_map")

        # Restore old unique constraint with tenant_id
        op.create_unique_constraint(
            "uq_tiger_resource",
            "tiger_resource_map",
            ["resource_type", "resource_id", "tenant_id"],
        )

        # Restore old index with tenant_id
        op.create_index(
            "idx_tiger_resource_lookup",
            "tiger_resource_map",
            ["tenant_id", "resource_type", "resource_id"],
        )
    else:
        # SQLite: Use batch_alter_table
        with op.batch_alter_table("tiger_resource_map") as batch_op:
            batch_op.add_column(
                sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default")
            )
            batch_op.drop_index("idx_tiger_resource_lookup")
            batch_op.drop_constraint("uq_tiger_resource", type_="unique")

            batch_op.create_unique_constraint(
                "uq_tiger_resource",
                ["resource_type", "resource_id", "tenant_id"],
            )
            batch_op.create_index(
                "idx_tiger_resource_lookup",
                ["tenant_id", "resource_type", "resource_id"],
            )
