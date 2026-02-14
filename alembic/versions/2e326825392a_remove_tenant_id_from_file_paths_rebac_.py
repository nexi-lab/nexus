"""remove_tenant_id_from_file_paths_rebac_only

Revision ID: 2e326825392a
Revises: 6563315727ab
Create Date: 2025-10-26 01:16:07.334335

This migration removes the tenant_id column from the file_paths table,
completing the migration to pure ReBAC-based multi-tenancy.

Background:
- Previous: Used database-level tenant isolation via file_paths.tenant_id
- Migration: Migrated to ReBAC-based isolation via rebac_tuples.zone_id
- Current: Completed migration by removing tenant_id from file_paths

Tenant isolation is now handled by:
1. OperationContext zone_id (passed per-operation)
2. rebac_tuples.zone_id (permission-level filtering)
3. Router validation (runtime tenant checking)

Files are no longer tenant-scoped at the database level.
All multi-tenant access control is enforced through ReBAC permissions.
"""

from collections.abc import Sequence
from contextlib import suppress
from typing import Union

import sqlalchemy as sa
from sqlalchemy.exc import OperationalError, ProgrammingError

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "2e326825392a"
down_revision: Union[str, Sequence[str], None] = "6563315727ab"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Intentionally duplicated from nexus.storage.views.VIEW_NAMES.
# Migrations must be self-contained snapshots — importing from application
# code would break if views.py changes after this migration is written.
_VIEW_NAMES = [
    "ready_work_items",
    "pending_work_items",
    "blocked_work_items",
    "work_by_priority",
    "in_progress_work",
    "ready_for_indexing",
    "hot_tier_eviction_candidates",
    "orphaned_content_objects",
]


def upgrade() -> None:
    """Remove tenant_id from file_paths table for pure ReBAC."""

    # Drop SQL views that reference fp.zone_id before modifying file_paths.
    # SQLite validates ALL views on any ALTER TABLE DROP COLUMN, so invalid
    # views would block unrelated schema changes during downgrade.
    for view_name in _VIEW_NAMES:
        op.execute(sa.text(f"DROP VIEW IF EXISTS {view_name}"))

    # batch_alter_table needed for SQLite (no native ALTER of constraints).
    # Each step uses a separate batch context because combining them causes
    # batch mode to try recreating indexes on dropped columns.
    with (
        suppress(OperationalError, ProgrammingError),
        op.batch_alter_table("file_paths") as batch_op,
    ):
        batch_op.drop_constraint("uq_tenant_virtual_path", type_="unique")

    with suppress(OperationalError, ProgrammingError):
        op.drop_index("idx_file_paths_tenant_id", table_name="file_paths")

    with (
        suppress(OperationalError, ProgrammingError),
        op.batch_alter_table("file_paths") as batch_op,
    ):
        batch_op.drop_column("tenant_id")

    with (
        suppress(OperationalError, ProgrammingError),
        op.batch_alter_table("file_paths") as batch_op,
    ):
        batch_op.create_unique_constraint("uq_virtual_path", ["virtual_path"])


def downgrade() -> None:
    """Restore tenant_id to file_paths table (for rollback to previous version)."""

    # batch_alter_table needed for SQLite (no native ALTER of constraints)
    with (
        suppress(OperationalError, ProgrammingError),
        op.batch_alter_table("file_paths") as batch_op,
    ):
        batch_op.drop_constraint("uq_virtual_path", type_="unique")

    # Re-add tenant_id column
    op.add_column("file_paths", sa.Column("tenant_id", sa.String(length=36), nullable=True))

    # Re-create index
    op.create_index("idx_file_paths_tenant_id", "file_paths", ["tenant_id"], unique=False)

    # Re-create old unique constraint
    with op.batch_alter_table("file_paths") as batch_op:
        batch_op.create_unique_constraint("uq_tenant_virtual_path", ["tenant_id", "virtual_path"])

    # Recreate SQL views if zone_id exists on file_paths (views reference fp.zone_id).
    # In migration-only databases, zone_id was never added via migration, so views
    # would be invalid. Only production databases using create_all() have zone_id.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("file_paths")}
    if "zone_id" in columns:
        from nexus.storage import views

        connection = op.get_bind()
        db_type = (
            "postgresql" if connection.dialect.name in ("postgresql", "postgres") else "sqlite"
        )
        for _name, view_sql in views.get_all_views(db_type):
            connection.execute(view_sql)
            connection.commit()
