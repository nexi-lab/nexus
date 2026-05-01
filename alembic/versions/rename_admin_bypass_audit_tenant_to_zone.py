"""Rename admin_bypass_audit.tenant_id -> zone_id

Revision ID: rename_bypass_tenant_to_zone
Revises: merge_approval_and_edge_schema
Create Date: 2026-05-01

The ``admin_bypass_audit`` table (added by ``a5bf12f44bc8``) was created
with a ``tenant_id`` column before the codebase-wide tenant -> zone
terminology migration. The application code in
``nexus.bricks.rebac.permissions_enhanced`` (insert + ``_ensure_tables``
DDL) writes ``zone_id`` and reads ``zone_id``, so every admin/system
bypass audit insert raises::

    psycopg2.errors.UndefinedColumn: column "zone_id" of relation
    "admin_bypass_audit" does not exist

on a freshly-bootstrapped database. ``_ensure_tables`` only runs when
the table does NOT exist, so it cannot self-heal an alembic-created
table that is already present with the old column.

Sibling rename migrations: ``rename_closure_tenant_to_zone``,
``2e326825392a`` (file_paths), ``3b2a1c5d7e8f`` (rebac_changelog),
``4f0aaaec2735`` / ``217a7e641338`` (rebac_tuples).

Strategy:
  * Drop the index that references ``tenant_id``
    (``idx_audit_tenant_timestamp``).
  * If the legacy unindexed ``ix_admin_bypass_audit_tenant_id`` index
    is present (created when the original migration set ``index=True``
    on the column), drop it too.
  * Rename the column ``tenant_id`` -> ``zone_id``.
  * Re-create ``idx_audit_zone_timestamp`` against ``zone_id`` to
    match ``permissions_enhanced._ensure_tables``.

SQLite is handled through ``batch_alter_table`` because it has no
native ``ALTER COLUMN`` support; alembic recreates the table behind
the scenes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "rename_bypass_tenant_to_zone"
down_revision: str | Sequence[str] | None = "merge_approval_and_edge_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return name in inspector.get_table_names()


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(idx["name"] == name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    """Rename tenant_id -> zone_id and rebuild the timestamp index."""
    if not _has_table("admin_bypass_audit"):
        # Table not yet created (a5bf12f44bc8 not applied) — nothing to do.
        return

    bind = op.get_bind()

    # Idempotency: prior partial run may have already renamed the column.
    if not _has_column("admin_bypass_audit", "tenant_id"):
        # Ensure the zone_id-based index exists even if rename ran before.
        if _has_column("admin_bypass_audit", "zone_id") and not _has_index(
            "admin_bypass_audit", "idx_audit_zone_timestamp"
        ):
            op.create_index(
                "idx_audit_zone_timestamp",
                "admin_bypass_audit",
                ["zone_id", "timestamp"],
            )
        return

    # 1. Drop indexes that reference tenant_id. Guarded so a partial prior run
    #    that already dropped them doesn't blow up.
    if _has_index("admin_bypass_audit", "idx_audit_tenant_timestamp"):
        op.drop_index("idx_audit_tenant_timestamp", table_name="admin_bypass_audit")
    if _has_index("admin_bypass_audit", "ix_admin_bypass_audit_tenant_id"):
        op.drop_index("ix_admin_bypass_audit_tenant_id", table_name="admin_bypass_audit")

    # 2. Rename the column. SQLite needs batch mode; PostgreSQL handles native
    #    RENAME COLUMN.
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("admin_bypass_audit") as batch_op:
            batch_op.alter_column("tenant_id", new_column_name="zone_id")
    else:
        op.alter_column("admin_bypass_audit", "tenant_id", new_column_name="zone_id")

    # 3. Recreate the zone-keyed timestamp index, matching the DDL in
    #    ``nexus.bricks.rebac.permissions_enhanced._ensure_tables``.
    if not _has_index("admin_bypass_audit", "idx_audit_zone_timestamp"):
        op.create_index(
            "idx_audit_zone_timestamp",
            "admin_bypass_audit",
            ["zone_id", "timestamp"],
        )


def downgrade() -> None:
    """Reverse: zone_id -> tenant_id and rebuild the tenant timestamp index."""
    if not _has_table("admin_bypass_audit"):
        return

    bind = op.get_bind()

    if not _has_column("admin_bypass_audit", "zone_id"):
        return

    if _has_index("admin_bypass_audit", "idx_audit_zone_timestamp"):
        op.drop_index("idx_audit_zone_timestamp", table_name="admin_bypass_audit")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("admin_bypass_audit") as batch_op:
            batch_op.alter_column("zone_id", new_column_name="tenant_id")
    else:
        op.alter_column("admin_bypass_audit", "zone_id", new_column_name="tenant_id")

    op.create_index(
        "idx_audit_tenant_timestamp",
        "admin_bypass_audit",
        ["tenant_id", "timestamp"],
    )
