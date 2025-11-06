"""add_embedding_support_to_memories_for_semantic_search

Revision ID: 7ab1369a10a9
Revises: 1181bd287cc1
Create Date: 2025-11-06 09:44:17.740663

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7ab1369a10a9"
down_revision: Union[str, Sequence[str], None] = "1181bd287cc1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema.

    Add embedding support for memory semantic search (#406):
    - embedding_model: Name of the embedding model used
    - embedding_dim: Dimension of the embedding vector
    - embedding: Vector embedding (stored as JSON array for SQLite, vector for PostgreSQL)
    """
    # Check if we're using PostgreSQL or SQLite
    bind = op.get_bind()
    dialect = bind.dialect.name

    # Add embedding metadata columns
    op.add_column("memories", sa.Column("embedding_model", sa.String(100), nullable=True))
    op.add_column("memories", sa.Column("embedding_dim", sa.Integer(), nullable=True))

    # Add embedding column based on database type
    if dialect == "postgresql":
        # For PostgreSQL, use native vector type if pgvector is available
        # Note: This requires pgvector extension to be installed
        # If not available, we'll store as JSON text
        try:
            # Try to create using vector type (pgvector)
            op.execute("CREATE EXTENSION IF NOT EXISTS vector")
            # Use TEXT for now, will be converted to vector type if needed
            op.add_column("memories", sa.Column("embedding", sa.Text(), nullable=True))
        except Exception:
            # Fallback to JSON text storage
            op.add_column("memories", sa.Column("embedding", sa.Text(), nullable=True))
    else:
        # For SQLite, store as JSON text
        op.add_column("memories", sa.Column("embedding", sa.Text(), nullable=True))

    # Add index on embedding_model for filtering
    op.create_index("idx_memory_embedding_model", "memories", ["embedding_model"])


def downgrade() -> None:
    """Downgrade schema."""
    # Drop index
    op.drop_index("idx_memory_embedding_model", table_name="memories")

    # Drop columns
    op.drop_column("memories", "embedding")
    op.drop_column("memories", "embedding_dim")
    op.drop_column("memories", "embedding_model")
