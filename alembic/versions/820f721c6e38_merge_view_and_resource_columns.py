"""merge_view_and_resource_columns

Revision ID: 820f721c6e38
Revises: 278a3d730040, 3c80651b81c6
Create Date: 2025-10-28 16:26:52.475531

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "820f721c6e38"
down_revision: Union[str, Sequence[str], None] = ("278a3d730040", "3c80651b81c6")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
