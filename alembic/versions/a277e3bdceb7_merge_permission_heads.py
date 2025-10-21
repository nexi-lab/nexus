"""merge_permission_heads

Revision ID: a277e3bdceb7
Revises: aadace49b644, f5a569506bfb
Create Date: 2025-10-21 11:11:54.365033

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "a277e3bdceb7"
down_revision: Union[str, Sequence[str], None] = ("aadace49b644", "f5a569506bfb")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
