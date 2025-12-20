"""add_api_key_to_users

Revision ID: f5462b917722
Revises: 42baa7b72b11
Create Date: 2025-12-19 02:16:53.921852

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f5462b917722"
down_revision: Union[str, Sequence[str], None] = "42baa7b72b11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add api_key and tenant_id columns to users table
    op.add_column("users", sa.Column("api_key", sa.String(length=255), nullable=True))
    op.add_column("users", sa.Column("tenant_id", sa.String(length=255), nullable=True))
    op.create_index("ix_users_api_key", "users", ["api_key"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    # Remove api_key and tenant_id columns
    op.drop_index("ix_users_api_key", table_name="users")
    op.drop_column("users", "tenant_id")
    op.drop_column("users", "api_key")
