"""add_state_field_to_memories_for_manual_approval

Memory State Management (Issue #368)

Adds 'state' field to memories table to support manual approval workflow:
- inactive: Newly created memories (pending review)
- active: Approved memories that are used in retrieval

This enables quality control, memory hygiene, and manual curation.

Revision ID: c86ab4d2ddae
Revises: a16e1db56def
Create Date: 2025-11-02 15:08:35.387657

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c86ab4d2ddae"
down_revision: Union[str, Sequence[str], None] = "a16e1db56def"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add state field to memories table for manual approval workflow."""

    # Add state column to memories table
    # Default to 'active' for backward compatibility (new memories immediately available)
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("state", sa.String(20), nullable=False, server_default="active")
        )

    # Create index for state queries
    op.create_index("idx_memory_state", "memories", ["state"])


def downgrade() -> None:
    """Remove state field from memories table."""

    # Drop state index
    op.drop_index("idx_memory_state", table_name="memories")

    # Remove state column
    with op.batch_alter_table("memories", schema=None) as batch_op:
        batch_op.drop_column("state")
