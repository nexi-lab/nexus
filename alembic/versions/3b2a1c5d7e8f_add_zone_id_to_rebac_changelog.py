"""add zone_id to rebac_changelog

The ``ReBACChangelogModel`` declares a non-nullable ``zone_id`` column
indexed for cache invalidation lookups, but the original create-table
migration (``a16e1db56def``) predates that field and no follow-up has
added it. Fresh databases initialised via ``alembic upgrade heads``
therefore lack the column, and any code path that writes a changelog
row (``rebac.utils.changelog.insert_changelog_entry`` — exercised by
``mounts add``) fails with::

    column "zone_id" of relation "rebac_changelog" does not exist

This migration adds the column nullable, backfills existing rows to the
root zone sentinel, then enforces NOT NULL with the model's default and
creates the supporting index. SQLite is handled via batch_alter_table.

Revision ID: 3b2a1c5d7e8f
Revises: 04188c0bbb28
Create Date: 2026-04-27 19:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "3b2a1c5d7e8f"
down_revision: str | Sequence[str] | None = "04188c0bbb28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROOT_ZONE_ID = "root"


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(c["name"] == column for c in inspector.get_columns(table))


def _has_index(table: str, name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(idx["name"] == name for idx in inspector.get_indexes(table))


def upgrade() -> None:
    """Add zone_id to rebac_changelog (idempotent)."""
    if not _has_column("rebac_changelog", "zone_id"):
        op.add_column(
            "rebac_changelog",
            sa.Column("zone_id", sa.String(255), nullable=True),
        )
        op.execute(
            sa.text(f"UPDATE rebac_changelog SET zone_id = '{ROOT_ZONE_ID}' WHERE zone_id IS NULL")
        )
        with op.batch_alter_table("rebac_changelog") as batch:
            batch.alter_column(
                "zone_id",
                existing_type=sa.String(255),
                nullable=False,
                server_default=ROOT_ZONE_ID,
            )

    if not _has_index("rebac_changelog", "ix_rebac_changelog_zone_id"):
        op.create_index(
            "ix_rebac_changelog_zone_id",
            "rebac_changelog",
            ["zone_id"],
        )


def downgrade() -> None:
    """Remove zone_id column and its index."""
    if _has_index("rebac_changelog", "ix_rebac_changelog_zone_id"):
        op.drop_index("ix_rebac_changelog_zone_id", table_name="rebac_changelog")
    if _has_column("rebac_changelog", "zone_id"):
        op.drop_column("rebac_changelog", "zone_id")
