"""merge migration_history and tenant_non_nullable heads

Revision ID: eb9a31742e51
Revises: add_migration_history, make_tenant_id_non_nullable
Create Date: 2026-01-09 23:30:00.060572

"""

from collections.abc import Sequence
from typing import Union

# revision identifiers, used by Alembic.
revision: str = "eb9a31742e51"
down_revision: Union[str, Sequence[str], None] = (
    "add_migration_history",
    "make_tenant_id_non_nullable",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
