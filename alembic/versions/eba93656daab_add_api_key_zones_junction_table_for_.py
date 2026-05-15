"""add api_key_zones junction table for #3785

Revision ID: eba93656daab
Revises: add_path_contexts_table
Create Date: 2026-04-24 20:59:38.773431

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers (leave alembic-generated values intact)
revision = "eba93656daab"
down_revision = "add_path_contexts_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Issue #3897: validate the zone-data invariant BEFORE any DDL. SQLite
    # doesn't roll back CREATE TABLE on transaction abort, so raising after
    # create_table would wedge the revision in a half-applied state where
    # the table exists, alembic_version is unchanged, and the operator
    # can't simply rerun (CREATE TABLE would fail "table exists"). Order
    # of operations: seed → preflight → DDL → backfill.

    bind = op.get_bind()

    # Seed the documented default ROOT_ZONE_ID="root" so runtime
    # create_api_key calls (e.g. POST /api/v2/agents/register) succeed on
    # a fresh install. Any other orphan zone_id on a live key is unknown
    # tenant state and must be resolved by a human, not silently blessed
    # as an Active zone (would bypass zone validation/reserved-name
    # rules).
    bind.execute(
        sa.text(
            """
            INSERT INTO zones (zone_id, name, phase, finalizers, created_at, updated_at)
            SELECT 'root', 'Root', 'Active', '[]',
                   CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            WHERE NOT EXISTS (SELECT 1 FROM zones WHERE zone_id = 'root')
            """
        )
    )

    # Pre-flight: refuse the upgrade when a live api_key references a
    # zone not present in zones (other than 'root', just seeded above).
    # Failing loudly here — before DDL — keeps the schema un-touched so
    # the operator can fix the data and rerun cleanly.
    orphans = (
        bind.execute(
            sa.text(
                """
                SELECT DISTINCT k.zone_id
                FROM api_keys k
                WHERE k.revoked = 0
                  AND k.zone_id IS NOT NULL
                  AND k.zone_id <> 'root'
                  AND NOT EXISTS (SELECT 1 FROM zones z WHERE z.zone_id = k.zone_id)
                ORDER BY k.zone_id
                """
            )
        )
        .scalars()
        .all()
    )
    if orphans:
        raise RuntimeError(
            "eba93656daab: live api_keys reference zone_ids with no matching "
            "zones row. Create the zones (or revoke the keys) before "
            f"re-running this migration. Offending zone_ids: {sorted(orphans)}"
        )

    op.create_table(
        "api_key_zones",
        sa.Column("key_id", sa.String(length=36), nullable=False),
        sa.Column("zone_id", sa.String(length=255), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.ForeignKeyConstraint(["key_id"], ["api_keys.key_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["zone_id"], ["zones.zone_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("key_id", "zone_id"),
    )
    op.create_index("idx_api_key_zones_key", "api_key_zones", ["key_id"])
    op.create_index("idx_api_key_zones_zone", "api_key_zones", ["zone_id"])

    # Backfill: every live token gets one junction row matching its current
    # primary zone_id. Idempotent set-based insert.
    op.execute(
        """
        INSERT INTO api_key_zones (key_id, zone_id, granted_at)
        SELECT key_id, zone_id, created_at FROM api_keys WHERE revoked = 0
        """
    )


def downgrade() -> None:
    op.drop_index("idx_api_key_zones_zone", table_name="api_key_zones")
    op.drop_index("idx_api_key_zones_key", table_name="api_key_zones")
    op.drop_table("api_key_zones")
