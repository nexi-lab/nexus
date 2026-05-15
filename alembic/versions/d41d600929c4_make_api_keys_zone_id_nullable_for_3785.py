"""make api_keys.zone_id nullable for #3785

Revision ID: d41d600929c4
Revises: 713294ffadf4
Create Date: 2026-04-25 09:14:30.392459

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d41d600929c4"
down_revision = "713294ffadf4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Junction table api_key_zones is now the source of truth for token→zone
    # mappings. APIKeyModel.zone_id remains as a backfill alias for the
    # junction's first row (so existing list_keys WHERE zone_id=? filters keep
    # working) but is no longer required — admin/zoneless keys may have NULL.
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "zone_id",
            existing_type=sa.String(length=255),
            nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("api_keys") as batch_op:
        batch_op.alter_column(
            "zone_id",
            existing_type=sa.String(length=255),
            nullable=False,
        )
