"""add_entity_metadata_to_entity_registry

Revision ID: add_entity_metadata
Revises: 928a619dabf4
Create Date: 2025-11-01 00:00:00.000000

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_entity_metadata"
down_revision: Union[str, Sequence[str], None] = "928a619dabf4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add entity_metadata column to entity_registry table
    op.add_column(
        "entity_registry",
        sa.Column("entity_metadata", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Remove entity_metadata column from entity_registry table
    op.drop_column("entity_registry", "entity_metadata")
