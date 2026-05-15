"""merge_retry_count_and_sequence_unique

Revision ID: f537c8b67980
Revises: add_retry_count_oplog, b8d2f1a3e567
Create Date: 2026-03-13 03:10:06.096170

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "f537c8b67980"
down_revision: Union[str, Sequence[str], None] = ("add_retry_count_oplog", "b8d2f1a3e567")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
