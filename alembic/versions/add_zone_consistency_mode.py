"""Add consistency_mode column to zones table (Issue #1180).

Adds a `consistency_mode` column (String(2), NOT NULL, default='SC') to the
`zones` table with a CHECK constraint limiting values to 'SC' (Strong
Consistency) and 'EC' (Eventual Consistency).

Existing zones get 'SC' as the default (server_default).

Revision ID: add_zone_consistency_mode
Revises: add_agent_records_table
Create Date: 2026-02-11
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_zone_consistency_mode"
down_revision: Union[str, Sequence[str], None] = "add_agent_events_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add consistency_mode column with CHECK constraint to zones table."""
    op.add_column(
        "zones",
        sa.Column(
            "consistency_mode",
            sa.String(2),
            nullable=False,
            server_default="SC",
        ),
    )
    # SQLite doesn't support ADD CONSTRAINT, use batch mode for compatibility
    with op.batch_alter_table("zones") as batch_op:
        batch_op.create_check_constraint(
            "ck_zones_consistency_mode",
            "consistency_mode IN ('SC', 'EC')",
        )


def downgrade() -> None:
    """Remove consistency_mode column from zones table."""
    with op.batch_alter_table("zones") as batch_op:
        batch_op.drop_constraint("ck_zones_consistency_mode", type_="check")
    op.drop_column("zones", "consistency_mode")
