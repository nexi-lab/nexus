"""merge backend_version and api_key heads

Revision ID: 0e1503c1dd79
Revises: add_backend_version_idx, f5462b917722
Create Date: 2025-12-19 15:54:54.711437

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "0e1503c1dd79"
down_revision: Union[str, Sequence[str], None] = ("add_backend_version_idx", "f5462b917722")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
