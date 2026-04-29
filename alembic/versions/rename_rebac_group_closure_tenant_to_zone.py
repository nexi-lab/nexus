"""Rename rebac_group_closure.tenant_id -> zone_id

Revision ID: rename_closure_tenant_to_zone
Revises: add_approval_decision_queue
Create Date: 2026-04-29

The ``rebac_group_closure`` table (added by ``add_leopard_closure``) was
created with a ``tenant_id`` column before the codebase-wide tenant -> zone
terminology migration. The application code (``nexus.bricks.rebac.cache.
leopard.LeopardCache`` and the Rust acceleration in
``_fetch_tuples_for_rust``) queries the table with ``WHERE zone_id = :zone_id``,
which causes every non-admin ReBAC permission check to raise::

    psycopg2.errors.UndefinedColumn: column "zone_id" does not exist

on a freshly-bootstrapped database.

Sibling migrations have already shipped the tenant -> zone rename for
``file_paths`` (``2e326825392a``), ``rebac_changelog`` (``3b2a1c5d7e8f``),
and ``rebac_tuples`` (``4f0aaaec2735`` / ``217a7e641338``). This migration
closes the remaining gap surfaced by Issue #3790's E2E coverage.

Strategy:
  * Drop the two indexes that reference ``tenant_id``
    (``idx_closure_member``, ``idx_closure_group``).
  * Rename the column ``tenant_id`` -> ``zone_id``. PostgreSQL's
    ``ALTER TABLE ... RENAME COLUMN`` cascades the rename through the
    primary-key constraint automatically, so the composite PK
    ``(member_type, member_id, group_type, group_id, tenant_id)`` becomes
    ``(member_type, member_id, group_type, group_id, zone_id)`` without
    a separate constraint rebuild.
  * Re-create the two indexes against ``zone_id``.

SQLite is handled through ``batch_alter_table`` because it has no native
``ALTER COLUMN`` support; alembic recreates the table behind the scenes.

The ``idx_closure_depth`` and ``idx_closure_updated_at_brin`` indexes are
untouched — they don't reference ``tenant_id``/``zone_id``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "rename_closure_tenant_to_zone"
down_revision: str | Sequence[str] | None = "add_approval_decision_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_column(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(idx["name"] == name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    """Rename tenant_id -> zone_id and rebuild the two referencing indexes."""
    bind = op.get_bind()

    # Idempotency: if a previous run already renamed the column, skip.
    if not _has_column("rebac_group_closure", "tenant_id"):
        return

    # 1. Drop indexes that reference tenant_id. Guarded so a partial prior run
    #    that already dropped them doesn't blow up.
    if _has_index("rebac_group_closure", "idx_closure_member"):
        op.drop_index("idx_closure_member", table_name="rebac_group_closure")
    if _has_index("rebac_group_closure", "idx_closure_group"):
        op.drop_index("idx_closure_group", table_name="rebac_group_closure")

    # 2. Rename the column. SQLite needs batch mode; PostgreSQL handles native
    #    RENAME COLUMN and auto-updates the PK constraint.
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("rebac_group_closure") as batch_op:
            batch_op.alter_column("tenant_id", new_column_name="zone_id")
    else:
        op.alter_column("rebac_group_closure", "tenant_id", new_column_name="zone_id")

    # 3. Recreate the two indexes against zone_id, matching the original
    #    column order from add_leopard_group_closure.py.
    op.create_index(
        "idx_closure_member",
        "rebac_group_closure",
        ["zone_id", "member_type", "member_id"],
    )
    op.create_index(
        "idx_closure_group",
        "rebac_group_closure",
        ["zone_id", "group_type", "group_id"],
    )


def downgrade() -> None:
    """Reverse: zone_id -> tenant_id and rebuild the two referencing indexes."""
    bind = op.get_bind()

    if not _has_column("rebac_group_closure", "zone_id"):
        return

    if _has_index("rebac_group_closure", "idx_closure_member"):
        op.drop_index("idx_closure_member", table_name="rebac_group_closure")
    if _has_index("rebac_group_closure", "idx_closure_group"):
        op.drop_index("idx_closure_group", table_name="rebac_group_closure")

    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("rebac_group_closure") as batch_op:
            batch_op.alter_column("zone_id", new_column_name="tenant_id")
    else:
        op.alter_column("rebac_group_closure", "zone_id", new_column_name="tenant_id")

    op.create_index(
        "idx_closure_member",
        "rebac_group_closure",
        ["tenant_id", "member_type", "member_id"],
    )
    op.create_index(
        "idx_closure_group",
        "rebac_group_closure",
        ["tenant_id", "group_type", "group_id"],
    )
