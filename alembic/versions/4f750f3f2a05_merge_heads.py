"""merge_heads

Revision ID: 4f750f3f2a05
Revises: t1234567890a, f5462b917722
Create Date: 2025-12-19 11:48:30.438050

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4f750f3f2a05'
down_revision: Union[str, Sequence[str], None] = ('t1234567890a', 'f5462b917722')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
