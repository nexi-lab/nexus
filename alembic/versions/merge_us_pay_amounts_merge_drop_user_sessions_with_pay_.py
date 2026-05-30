"""merge_drop_user_sessions_with_pay_amounts

Revision ID: merge_us_pay_amounts
Revises: drop_user_sessions_table, pay_amounts_micro_units
Create Date: 2026-05-31 02:02:09.867420

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "merge_us_pay_amounts"
down_revision: Union[str, Sequence[str], None] = (
    "drop_user_sessions_table",
    "pay_amounts_micro_units",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
