"""Add agent_records table (Agent OS Phase 1, Issue #1240).

Creates the agent_records table for agent lifecycle tracking with:
- Session generation counter for optimistic locking
- State machine column (UNKNOWN, CONNECTED, IDLE, SUSPENDED)
- Heartbeat timestamp for stale detection
- Composite indexes for zone+state queries and stale detection

Revision ID: add_agent_records_table
Revises: add_memory_version_tracking
Create Date: 2026-02-09
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_agent_records_table"
down_revision: Union[str, Sequence[str], None] = "add_memory_version_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent_records table with composite indexes."""
    op.create_table(
        "agent_records",
        sa.Column("agent_id", sa.String(255), primary_key=True, nullable=False),
        sa.Column("owner_id", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(100), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="UNKNOWN"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_heartbeat", sa.DateTime(), nullable=True),
        sa.Column("agent_metadata", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )

    # Composite indexes (Decision #15A)
    op.create_index(
        "idx_agent_records_zone_state", "agent_records", ["zone_id", "state"]
    )
    op.create_index(
        "idx_agent_records_state_heartbeat",
        "agent_records",
        ["state", "last_heartbeat"],
    )
    op.create_index("idx_agent_records_owner", "agent_records", ["owner_id"])


def downgrade() -> None:
    """Remove agent_records table and indexes."""
    op.drop_index("idx_agent_records_owner", table_name="agent_records")
    op.drop_index("idx_agent_records_state_heartbeat", table_name="agent_records")
    op.drop_index("idx_agent_records_zone_state", table_name="agent_records")
    op.drop_table("agent_records")
