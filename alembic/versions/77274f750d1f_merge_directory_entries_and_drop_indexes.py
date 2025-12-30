"""merge_directory_entries_and_drop_indexes

Revision ID: 77274f750d1f
Revises: add_directory_entries_table, drop_ix_duplicate_indexes
Create Date: 2025-12-29 23:53:15.613091

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77274f750d1f'
down_revision: Union[str, Sequence[str], None] = ('add_directory_entries_table', 'drop_ix_duplicate_indexes')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
