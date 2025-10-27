"""Remove UNIX permissions from memories table

Revision ID: remove_memory_unix_permissions
Revises: eb7fb97cf314
Create Date: 2025-10-25 21:30:00.000000

"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "remove_memory_unix_permissions"
down_revision = "04a22b67d228"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove UNIX-style group and mode columns from memories table."""
    # Remove columns that are no longer used in ReBAC-based permissions
    op.drop_column("memories", "group")
    op.drop_column("memories", "mode")


def downgrade() -> None:
    """Re-add UNIX-style group and mode columns to memories table."""
    op.add_column("memories", sa.Column("group", sa.String(255), nullable=True))
    op.add_column("memories", sa.Column("mode", sa.Integer(), nullable=False, server_default="420"))
