"""Add inherit_permissions column to api_keys

Revision ID: add_inherit_perms
Revises: add_sync_jobs
Create Date: 2025-12-12

Adds inherit_permissions column to api_keys table to control whether
agents inherit permissions from their owners.

Three permission modes for agents:
1. No API key → Uses owner's credentials → Full permissions (existing)
2. Has API key + inherit_permissions=0 → Zero permissions (explicit grants only)
3. Has API key + inherit_permissions=1 → Full owner permissions (opt-in)

Migration strategy:
- Existing agent keys default to inherit_permissions=1 (backward compatible)
- New agent keys default to inherit_permissions=0 (principle of least privilege)
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_inherit_perms"
down_revision: Union[str, Sequence[str], None] = "add_sync_jobs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add inherit_permissions column to api_keys table.

    v0.5.1: Permission inheritance control
    - inherit_permissions: Whether agent inherits owner's permissions (0=no, 1=yes)
    - Default 1 for existing keys (backward compatible)
    - Default 0 for new keys (principle of least privilege)
    """
    # Add inherit_permissions column with default 1 for backward compatibility
    # The server_default="1" ensures all existing rows get value 1
    op.add_column(
        "api_keys",
        sa.Column(
            "inherit_permissions",
            sa.Integer,
            nullable=False,
            server_default="1",  # All existing keys (including agents) inherit by default
            comment="Whether agent inherits owner's permissions (0=no, 1=yes)",
        ),
    )


def downgrade() -> None:
    """Remove inherit_permissions column."""
    op.drop_column("api_keys", "inherit_permissions")
