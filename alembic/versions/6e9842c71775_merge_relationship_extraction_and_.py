"""merge relationship extraction and performance fixes

Revision ID: 6e9842c71775
Revises: add_relationship_extraction, eb9a31742e51
Create Date: 2026-01-10 19:08:41.376567

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "6e9842c71775"
down_revision: Union[str, Sequence[str], None] = ("add_relationship_extraction", "eb9a31742e51")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
