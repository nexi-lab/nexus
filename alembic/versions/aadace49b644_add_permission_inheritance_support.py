"""add_permission_inheritance_support

Revision ID: aadace49b644
Revises: 928a619dabf4
Create Date: 2025-10-20 23:35:52.896172

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aadace49b644"
down_revision: Union[str, Sequence[str], None] = "928a619dabf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add is_default column to acl_entries table for default ACL entries
    # Default ACL entries are inherited by child files/directories
    op.add_column(
        "acl_entries", sa.Column("is_default", sa.Boolean(), nullable=False, server_default="0")
    )
    op.create_index("idx_acl_entries_is_default", "acl_entries", ["is_default"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Remove is_default column and index
    op.drop_index("idx_acl_entries_is_default", table_name="acl_entries")
    op.drop_column("acl_entries", "is_default")
