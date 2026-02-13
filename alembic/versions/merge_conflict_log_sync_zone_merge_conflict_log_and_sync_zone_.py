"""Merge conflict_log and sync_zone_consistency heads

Revision ID: merge_conflict_log_sync_zone
Revises: dcdd78abdc93
Create Date: 2026-02-12 07:12:37.819652

Note: Original down_revision was ("add_conflict_log", "merge_sync_zone_consistency")
but dcdd78abdc93 already merges both of those plus fad1973e5a88. Linearized to
avoid pathological Alembic topological sort (Issue #1296).
"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_conflict_log_sync_zone"
down_revision: Union[str, Sequence[str], None] = "dcdd78abdc93"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
