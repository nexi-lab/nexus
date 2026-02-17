"""merge_context_branches_and_dlq_seq

Revision ID: 101ea1909580
Revises: add_context_branches, add_seq_number_dlq
Create Date: 2026-02-17 15:23:29.355298

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "101ea1909580"
down_revision: Union[str, Sequence[str], None] = ("add_context_branches", "add_seq_number_dlq")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
