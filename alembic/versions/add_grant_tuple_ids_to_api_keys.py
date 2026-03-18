"""Add grant_tuple_ids column to api_keys

Revision ID: add_grant_tuple_ids
Revises: update_file_namespace_shared
Create Date: 2026-03-17

Stores JSON array of ReBAC tuple IDs created as grants for this key,
enabling targeted cleanup on key revocation (Issue #3128).
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_grant_tuple_ids"
down_revision: Union[str, Sequence[str], None] = "a3062sec01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add grant_tuple_ids column to api_keys table."""
    op.add_column(
        "api_keys",
        sa.Column(
            "grant_tuple_ids",
            sa.Text,
            nullable=True,
            comment="JSON array of ReBAC tuple IDs created as grants for this key",
        ),
    )


def downgrade() -> None:
    """Remove grant_tuple_ids column."""
    op.drop_column("api_keys", "grant_tuple_ids")
