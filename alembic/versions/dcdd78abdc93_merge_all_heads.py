"""merge_all_heads

Revision ID: dcdd78abdc93
Revises: add_conflict_log, fad1973e5a88, merge_sync_zone_consistency
Create Date: 2026-02-12 03:34:31.110596

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dcdd78abdc93'
down_revision: Union[str, Sequence[str], None] = ('add_conflict_log', 'fad1973e5a88', 'merge_sync_zone_consistency')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
