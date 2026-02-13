"""Make tenant_id non-nullable for multi-tenant security

Revision ID: make_tenant_id_non_nullable
Revises: merge_posix_tiger
Create Date: 2026-01-03

Issue #773: Make tenant_id required (non-nullable) for multi-tenant isolation

This migration:
1. Backfills NULL tenant_id values to 'default' for existing data
2. Adds NOT NULL constraint to tenant_id columns

Security Impact:
- Prevents accidental cross-tenant data access when tenant_id is not provided
- Makes tenant isolation explicit rather than relying on fallback logic
- Matches Supabase-style strict row-level security

Affected Tables:
- file_paths
- directory_entries
- rebac_tuples (tenant_id, subject_tenant_id, object_tenant_id)
- rebac_changelog
- rebac_check_cache
- api_keys
- mount_configs
- memories
- playbooks
- trajectories
- user_sessions
- oauth_credentials
- content_cache

Note: Cross-tenant sharing still works via subject_tenant_id != object_tenant_id
for relations in CROSS_TENANT_ALLOWED_RELATIONS.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "make_tenant_id_non_nullable"
down_revision: Union[str, Sequence[str], None] = "merge_posix_tiger"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Tables with tenant_id that need to be made non-nullable
TABLES_WITH_TENANT_ID = [
    "file_paths",
    "operation_log",
    "rebac_changelog",
    "rebac_check_cache",
    "api_keys",
    "mount_configs",
    "memories",
    "playbooks",
    "trajectories",
    "user_sessions",
    "oauth_credentials",
    "content_cache",
]

# Tables with multiple tenant_id columns
REBAC_TENANT_COLUMNS = ["tenant_id", "subject_tenant_id", "object_tenant_id"]


def _table_has_column(inspector, table: str, column: str) -> bool:
    """Check if a table exists and has the given column."""
    try:
        columns = {c["name"] for c in inspector.get_columns(table)}
    except sa.exc.NoSuchTableError:
        return False
    return column in columns


def upgrade() -> None:
    """Make tenant_id columns non-nullable after backfilling."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    # Step 1: Backfill NULL tenant_id values to 'default'
    for table in TABLES_WITH_TENANT_ID:
        if _table_has_column(inspector, table, "tenant_id"):
            bind.execute(
                sa.text(f"UPDATE {table} SET tenant_id = 'default' WHERE tenant_id IS NULL")
            )

    # Backfill rebac_tuples (has 3 tenant columns)
    for col in REBAC_TENANT_COLUMNS:
        if _table_has_column(inspector, "rebac_tuples", col):
            bind.execute(sa.text(f"UPDATE rebac_tuples SET {col} = 'default' WHERE {col} IS NULL"))

    # Backfill directory_entries (tenant_id is part of composite primary key)
    if _table_has_column(inspector, "directory_entries", "tenant_id"):
        bind.execute(
            sa.text("UPDATE directory_entries SET tenant_id = 'default' WHERE tenant_id IS NULL")
        )

    # Step 2: Add NOT NULL constraint
    if is_postgres:
        # PostgreSQL: Use ALTER COLUMN SET NOT NULL
        for table in TABLES_WITH_TENANT_ID:
            if _table_has_column(inspector, table, "tenant_id"):
                op.alter_column(
                    table,
                    "tenant_id",
                    existing_type=sa.String(255),
                    nullable=False,
                )

        # rebac_tuples: Make all 3 tenant columns non-nullable
        for col in REBAC_TENANT_COLUMNS:
            if _table_has_column(inspector, "rebac_tuples", col):
                op.alter_column(
                    "rebac_tuples",
                    col,
                    existing_type=sa.String(255),
                    nullable=False,
                )

        # directory_entries: tenant_id is primary key, already effectively non-null
        # But we make it explicit
        if _table_has_column(inspector, "directory_entries", "tenant_id"):
            op.alter_column(
                "directory_entries",
                "tenant_id",
                existing_type=sa.String(255),
                nullable=False,
            )

    else:
        # SQLite: Use batch_alter_table (recreates table)
        for table in TABLES_WITH_TENANT_ID:
            if _table_has_column(inspector, table, "tenant_id"):
                with op.batch_alter_table(table) as batch_op:
                    batch_op.alter_column(
                        "tenant_id",
                        existing_type=sa.String(255),
                        nullable=False,
                    )

        if _table_has_column(inspector, "rebac_tuples", "tenant_id"):
            with op.batch_alter_table("rebac_tuples") as batch_op:
                for col in REBAC_TENANT_COLUMNS:
                    batch_op.alter_column(
                        col,
                        existing_type=sa.String(255),
                        nullable=False,
                    )

        if _table_has_column(inspector, "directory_entries", "tenant_id"):
            with op.batch_alter_table("directory_entries") as batch_op:
                batch_op.alter_column(
                    "tenant_id",
                    existing_type=sa.String(255),
                    nullable=False,
                )


def downgrade() -> None:
    """Revert tenant_id columns to nullable."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        for table in TABLES_WITH_TENANT_ID:
            if _table_has_column(inspector, table, "tenant_id"):
                op.alter_column(
                    table,
                    "tenant_id",
                    existing_type=sa.String(255),
                    nullable=True,
                )

        for col in REBAC_TENANT_COLUMNS:
            if _table_has_column(inspector, "rebac_tuples", col):
                op.alter_column(
                    "rebac_tuples",
                    col,
                    existing_type=sa.String(255),
                    nullable=True,
                )

        if _table_has_column(inspector, "directory_entries", "tenant_id"):
            op.alter_column(
                "directory_entries",
                "tenant_id",
                existing_type=sa.String(255),
                nullable=True,
            )

    else:
        for table in TABLES_WITH_TENANT_ID:
            if _table_has_column(inspector, table, "tenant_id"):
                with op.batch_alter_table(table) as batch_op:
                    batch_op.alter_column(
                        "tenant_id",
                        existing_type=sa.String(255),
                        nullable=True,
                    )

        if _table_has_column(inspector, "rebac_tuples", "tenant_id"):
            with op.batch_alter_table("rebac_tuples") as batch_op:
                for col in REBAC_TENANT_COLUMNS:
                    batch_op.alter_column(
                        col,
                        existing_type=sa.String(255),
                        nullable=True,
                    )

        if _table_has_column(inspector, "directory_entries", "tenant_id"):
            with op.batch_alter_table("directory_entries") as batch_op:
                batch_op.alter_column(
                    "tenant_id",
                    existing_type=sa.String(255),
                    nullable=True,
                )
