"""merge subscriptions and user auth heads

Revision ID: 42baa7b72b11
Revises: add_subscriptions_table, u1234567890a
Create Date: 2025-12-19 01:30:12.458852

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '42baa7b72b11'
down_revision: Union[str, Sequence[str], None] = ('add_subscriptions_table', 'u1234567890a')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
