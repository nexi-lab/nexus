"""Merge conflict_log and sync_zone_consistency heads

Revision ID: merge_conflict_log_sync_zone
Revises: add_conflict_log, merge_sync_zone_consistency
Create Date: 2026-02-12 07:12:37.819652

"""
from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = 'merge_conflict_log_sync_zone'
down_revision: Union[str, Sequence[str], None] = ('add_conflict_log', 'merge_sync_zone_consistency')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
