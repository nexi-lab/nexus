"""merge_heads

Revision ID: b02814593b71
Revises: add_rebac_partial_indexes, content_cache_use_bytea
Create Date: 2025-12-20 03:10:58.231774

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "b02814593b71"
down_revision: Union[str, Sequence[str], None] = (
    "add_rebac_partial_indexes",
    "content_cache_use_bytea",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
