"""merge tiger_cache and user_model heads

Revision ID: eaa8b9fa1b89
Revises: add_tiger_cache, 0e1503c1dd79
Create Date: 2025-12-19 15:55:36.273673

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "eaa8b9fa1b89"
down_revision: Union[str, Sequence[str], None] = ("add_tiger_cache", "0e1503c1dd79")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
