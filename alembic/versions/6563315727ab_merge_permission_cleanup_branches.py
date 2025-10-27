"""merge_permission_cleanup_branches

Revision ID: 6563315727ab
Revises: remove_memory_unix_permissions, f1234567890a
Create Date: 2025-10-26 01:16:02.985157

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "6563315727ab"
down_revision: Union[str, Sequence[str], None] = ("remove_memory_unix_permissions", "f1234567890a")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
