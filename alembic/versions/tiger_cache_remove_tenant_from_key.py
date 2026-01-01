"""Remove tenant_id from Tiger Cache unique constraint

Revision ID: tiger_cache_remove_tenant
Revises: convert_vector_to_halfvec
Create Date: 2025-01-01

Issue #979: Tiger Cache persistence and cross-tenant optimization

This migration removes tenant_id from the Tiger Cache unique constraint
to allow shared resources (e.g., /skills in 'default' tenant) to be
cached across tenants without cache misses.

Changes:
- Drop old unique constraint (subject_type, subject_id, permission, resource_type, tenant_id)
- Create new unique constraint (subject_type, subject_id, permission, resource_type)
- Update index to remove tenant_id

Note: The tenant_id column is kept for backward compatibility but is no longer
part of the cache key. Tenant isolation is enforced during permission computation.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "tiger_cache_remove_tenant"
down_revision: Union[str, Sequence[str], None] = "convert_vector_to_halfvec"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove tenant_id from Tiger Cache unique constraint."""
    bind = op.get_bind()

    # Drop old unique constraint
    if bind.dialect.name == "postgresql":
        # Step 1: Delete duplicate rows (keep only most recent per new key)
        # This is necessary because removing tenant_id from constraint may create duplicates
        bind.execute(
            sa.text("""
            DELETE FROM tiger_cache t1
            USING tiger_cache t2
            WHERE t1.cache_id < t2.cache_id
              AND t1.subject_type = t2.subject_type
              AND t1.subject_id = t2.subject_id
              AND t1.permission = t2.permission
              AND t1.resource_type = t2.resource_type
        """)
        )

        # PostgreSQL: Drop constraint by name
        op.drop_constraint("uq_tiger_cache", "tiger_cache", type_="unique")

        # Drop old index
        op.drop_index("idx_tiger_cache_lookup", table_name="tiger_cache")

        # Create new unique constraint without tenant_id
        op.create_unique_constraint(
            "uq_tiger_cache",
            "tiger_cache",
            ["subject_type", "subject_id", "permission", "resource_type"],
        )

        # Create new index without tenant_id
        op.create_index(
            "idx_tiger_cache_lookup",
            "tiger_cache",
            ["subject_type", "subject_id", "permission", "resource_type"],
        )
    else:
        # SQLite: Need to recreate table (SQLite doesn't support DROP CONSTRAINT)
        # For SQLite, we'll use batch_alter_table
        with op.batch_alter_table("tiger_cache") as batch_op:
            # Drop old index
            batch_op.drop_index("idx_tiger_cache_lookup")

            # Drop and recreate unique constraint
            batch_op.drop_constraint("uq_tiger_cache", type_="unique")
            batch_op.create_unique_constraint(
                "uq_tiger_cache",
                ["subject_type", "subject_id", "permission", "resource_type"],
            )

            # Create new index
            batch_op.create_index(
                "idx_tiger_cache_lookup",
                ["subject_type", "subject_id", "permission", "resource_type"],
            )


def downgrade() -> None:
    """Restore tenant_id to Tiger Cache unique constraint."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        # PostgreSQL: Drop new constraint and restore old
        op.drop_constraint("uq_tiger_cache", "tiger_cache", type_="unique")
        op.drop_index("idx_tiger_cache_lookup", table_name="tiger_cache")

        # Restore old unique constraint with tenant_id
        op.create_unique_constraint(
            "uq_tiger_cache",
            "tiger_cache",
            ["subject_type", "subject_id", "permission", "resource_type", "tenant_id"],
        )

        # Restore old index with tenant_id
        op.create_index(
            "idx_tiger_cache_lookup",
            "tiger_cache",
            ["tenant_id", "subject_type", "subject_id", "permission", "resource_type"],
        )
    else:
        # SQLite: Use batch_alter_table
        with op.batch_alter_table("tiger_cache") as batch_op:
            batch_op.drop_index("idx_tiger_cache_lookup")
            batch_op.drop_constraint("uq_tiger_cache", type_="unique")

            batch_op.create_unique_constraint(
                "uq_tiger_cache",
                ["subject_type", "subject_id", "permission", "resource_type", "tenant_id"],
            )

            batch_op.create_index(
                "idx_tiger_cache_lookup",
                ["tenant_id", "subject_type", "subject_id", "permission", "resource_type"],
            )
