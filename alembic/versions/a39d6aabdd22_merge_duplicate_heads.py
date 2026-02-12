"""merge_duplicate_heads

Revision ID: a39d6aabdd22
Revises: dcdd78abdc93, merge_conflict_log_sync_zone
Create Date: 2026-02-12 20:59:49.822531

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "a39d6aabdd22"
down_revision: Union[str, Sequence[str], None] = ("dcdd78abdc93", "merge_conflict_log_sync_zone")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
