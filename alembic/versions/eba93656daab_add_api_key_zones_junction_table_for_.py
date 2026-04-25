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

    # Issue #3897: ensure every zone_id referenced by a live api_keys row
    # exists in `zones` BEFORE the backfill below — otherwise the junction
    # FK to zones.zone_id rejects the insert. Always seed the documented
    # default ROOT_ZONE_ID="root" so runtime create_api_key calls (e.g.
    # POST /api/v2/agents/register) succeed on a fresh install.
    op.execute(
        """
        INSERT INTO zones (zone_id, name, phase, finalizers, created_at, updated_at)
        SELECT 'root', 'Root', 'Active', '[]',
               CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
        WHERE NOT EXISTS (SELECT 1 FROM zones WHERE zone_id = 'root')
        """
    )
    # Pre-flight: refuse to backfill when a live api_key references a zone
    # that doesn't exist in zones (other than 'root', just seeded above).
    # Auto-creating those rows would silently bless arbitrary historical or
    # corrupt zone strings as Active tenants, bypassing zone validation and
    # reserved-name rules. Fail loudly with an actionable list so the
    # operator can create the zone or revoke the key before re-running.
    bind = op.get_bind()
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
