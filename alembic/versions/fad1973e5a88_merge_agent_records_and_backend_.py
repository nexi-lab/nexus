"""merge_agent_records_and_backend_changelog_heads

Revision ID: fad1973e5a88
Revises: add_agent_records_table, add_backend_change_log
Create Date: 2026-02-11 16:21:09.814112

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "fad1973e5a88"
down_revision: Union[str, Sequence[str], None] = (
    "add_agent_records_table",
    "add_backend_change_log",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
