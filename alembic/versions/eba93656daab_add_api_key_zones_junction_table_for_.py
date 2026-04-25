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
