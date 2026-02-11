"""Add agent_events audit log table (Issue #1307).

Creates the agent_events table for recording agent lifecycle events
(sandbox creation, connection, termination). Append-only audit log
used by SandboxAuthService.

Revision ID: add_agent_events_table
Revises: add_agent_records_table
Create Date: 2026-02-11
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_agent_events_table"
down_revision: Union[str, Sequence[str], None] = "add_agent_records_table"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add agent_events table with indexes."""
    op.create_table(
        "agent_events",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("zone_id", sa.String(100), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_agent_events_agent_created",
        "agent_events",
        ["agent_id", "created_at"],
    )
    op.create_index(
        "ix_agent_events_type",
        "agent_events",
        ["event_type"],
    )


def downgrade() -> None:
    """Remove agent_events table."""
    op.drop_index("ix_agent_events_type", table_name="agent_events")
    op.drop_index("ix_agent_events_agent_created", table_name="agent_events")
    op.drop_table("agent_events")
