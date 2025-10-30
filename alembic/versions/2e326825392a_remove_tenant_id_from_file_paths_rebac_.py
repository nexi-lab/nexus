"""remove_tenant_id_from_file_paths_rebac_only

Revision ID: 2e326825392a
Revises: 6563315727ab
Create Date: 2025-10-26 01:16:07.334335

This migration removes the tenant_id column from the file_paths table,
completing the migration to pure ReBAC-based multi-tenancy.

Background:
- Previous: Used database-level tenant isolation via file_paths.tenant_id
- Migration: Migrated to ReBAC-based isolation via rebac_tuples.tenant_id
- Current: Completed migration by removing tenant_id from file_paths

Tenant isolation is now handled by:
1. OperationContext.tenant_id (passed per-operation)
2. rebac_tuples.tenant_id (permission-level filtering)
3. Router validation (runtime tenant checking)

Files are no longer tenant-scoped at the database level.
All multi-tenant access control is enforced through ReBAC permissions.
"""

from collections.abc import Sequence
from contextlib import suppress
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e326825392a"
down_revision: Union[str, Sequence[str], None] = "6563315727ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove tenant_id from file_paths table for pure ReBAC."""

    # Step 1: Drop the old unique constraint (tenant_id, virtual_path)
    # This constraint was used for database-level tenant isolation
    with suppress(Exception):  # Constraint may not exist in newer databases
        op.drop_constraint("uq_tenant_virtual_path", "file_paths", type_="unique")

    # Step 2: Drop the tenant_id index
    with suppress(Exception):  # Index may not exist
        op.drop_index("idx_file_paths_tenant_id", table_name="file_paths")

    # Step 3: Drop the tenant_id column
    # NOTE: This is safe because current code doesn't use this column
    # All tenant isolation is now handled via ReBAC
    with suppress(Exception):  # Column may not exist
        op.drop_column("file_paths", "tenant_id")

    # Step 4: Create new unique constraint on virtual_path only
    # This matches the current model definition (see models.py:91)
    with suppress(Exception):  # Constraint may already exist
        op.create_unique_constraint("uq_virtual_path", "file_paths", ["virtual_path"])


def downgrade() -> None:
    """Restore tenant_id to file_paths table (for rollback to previous version)."""

    # Step 1: Drop the new unique constraint
    with suppress(Exception):
        op.drop_constraint("uq_virtual_path", "file_paths", type_="unique")

    # Step 2: Re-add tenant_id column
    # NOTE: Setting nullable=True to allow migration of existing data
    # In v0.4.x, this was NOT NULL, but we can't enforce that during rollback
    op.add_column("file_paths", sa.Column("tenant_id", sa.String(length=36), nullable=True))

    # Step 3: Re-create index
    op.create_index("idx_file_paths_tenant_id", "file_paths", ["tenant_id"], unique=False)

    # Step 4: Re-create old unique constraint
    # NOTE: This will fail if there are duplicate paths across tenants
    # Users must manually clean up data before downgrading
    op.create_unique_constraint(
        "uq_tenant_virtual_path", "file_paths", ["tenant_id", "virtual_path"]
    )
