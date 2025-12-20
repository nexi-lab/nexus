"""merge tenant_management, rebac_indexes, and user_model heads

Revision ID: dfb85dcdc557
Revises: 4f750f3f2a05, add_rebac_partial_indexes, eaa8b9fa1b89
Create Date: 2025-12-19 18:01:13.292821

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "dfb85dcdc557"
down_revision: Union[str, Sequence[str], None] = (
    "4f750f3f2a05",
    "add_rebac_partial_indexes",
    "eaa8b9fa1b89",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
