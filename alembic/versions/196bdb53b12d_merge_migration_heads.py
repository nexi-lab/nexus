"""merge_migration_heads

Revision ID: 196bdb53b12d
Revises: add_entity_metadata, c86ab4d2ddae
Create Date: 2025-11-02 15:24:19.761191

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "196bdb53b12d"
down_revision: Union[str, Sequence[str], None] = ("add_entity_metadata", "c86ab4d2ddae")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
