"""add permissions to api_key_zones for #3785

Revision ID: 713294ffadf4
Revises: eba93656daab
Create Date: 2026-04-25 08:42:00.458675

"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

# revision identifiers (leave alembic-generated values intact)
revision = "713294ffadf4"
down_revision = "eba93656daab"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_key_zones",
        sa.Column("permissions", sa.String(length=8), nullable=False, server_default="rw"),
    )


def downgrade() -> None:
    op.drop_column("api_key_zones", "permissions")
