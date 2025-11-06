"""remove_sandbox_name_uniqueness_constraint_for_get_or_create

Revision ID: 1181bd287cc1
Revises: a1b2c3d4e5f6
Create Date: 2025-11-05 18:01:56.262417

"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1181bd287cc1"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Remove unique constraint on (user_id, name) to allow name reuse for stopped sandboxes.

    This enables sandbox_get_or_create() to properly reuse sandbox names when
    the existing sandbox is stopped. The application layer will enforce uniqueness
    for active sandboxes only.
    """
    # Drop the unique constraint
    op.drop_constraint("uq_sandbox_user_name", "sandbox_metadata", type_="unique")


def downgrade() -> None:
    """Restore unique constraint on (user_id, name)."""
    # Re-create the unique constraint
    op.create_unique_constraint("uq_sandbox_user_name", "sandbox_metadata", ["user_id", "name"])
