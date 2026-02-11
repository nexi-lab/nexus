"""Merge sync_backlog and zone_consistency_mode heads.

Two independent features added migrations with different parents:
- add_sync_backlog (Issue #1129) from add_backend_change_log
- add_zone_consistency_mode (Issue #1180) from add_agent_records_table

This merge migration unifies them into a single head.

Revision ID: merge_sync_zone_consistency
Revises: add_sync_backlog, add_zone_consistency_mode
Create Date: 2026-02-11
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_sync_zone_consistency"
down_revision: Union[str, Sequence[str], None] = (
    "add_sync_backlog",
    "add_zone_consistency_mode",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge migration — no schema changes needed."""
    pass


def downgrade() -> None:
    """Merge migration — no schema changes needed."""
    pass
