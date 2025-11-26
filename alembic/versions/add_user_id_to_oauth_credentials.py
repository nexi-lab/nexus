"""Add user_id field to oauth_credentials table

Revision ID: add_user_id_oauth
Revises: 3e3663b4e99a
Create Date: 2025-11-26 00:00:00

Adds user_id field to oauth_credentials table for proper user identity tracking.
This enables per-user isolation when user_id != user_email.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_user_id_oauth"
down_revision: Union[str, Sequence[str], None] = "3e3663b4e99a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add user_id column to oauth_credentials table.

    This field stores the Nexus user identity (for permission checks),
    which may differ from user_email (OAuth provider email, tied to tokens).
    """
    # Add user_id column (nullable, indexed for efficient filtering)
    op.add_column(
        "oauth_credentials",
        sa.Column("user_id", sa.String(length=255), nullable=True),
    )

    # Create index for user_id (for efficient filtering by user)
    op.create_index(
        "idx_oauth_user_id",
        "oauth_credentials",
        ["user_id"],
        unique=False,
    )

    # Note: We don't populate user_id from user_email here because:
    # 1. user_id comes from OperationContext (API key authentication)
    # 2. Existing credentials may not have a valid user_id mapping
    # 3. New credentials will have user_id set during storage via context
    # If needed, a data migration can be run separately to populate user_id
    # from other sources (e.g., api_keys table based on created_by field)


def downgrade() -> None:
    """Remove user_id column and index from oauth_credentials table."""
    op.drop_index("idx_oauth_user_id", table_name="oauth_credentials")
    op.drop_column("oauth_credentials", "user_id")
