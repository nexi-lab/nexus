"""Add content_cache table for connector caching

Revision ID: add_content_cache
Revises: add_user_id_oauth
Create Date: 2025-11-28

Adds content_cache table for caching connector content (GCS, X, Gmail, etc.)
to enable fast grep, glob, and semantic search without real-time connector access.

See docs/design/cache-layer.md for design details.
Part of: #505, #510 (cache layer epic)
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_content_cache"
down_revision: Union[str, Sequence[str], None] = "add_user_id_oauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create content_cache table."""
    # Create the content_cache table
    op.create_table(
        "content_cache",
        # Primary key
        sa.Column("cache_id", sa.String(36), primary_key=True),
        # References
        sa.Column(
            "path_id",
            sa.String(36),
            sa.ForeignKey("file_paths.path_id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        # Tenant isolation
        sa.Column("tenant_id", sa.String(255), nullable=True),
        # Content storage
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_binary", sa.Text(), nullable=True),  # Base64 encoded
        sa.Column("content_hash", sa.String(64), nullable=False),
        # Size tracking
        sa.Column("original_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("cached_size_bytes", sa.BigInteger(), nullable=False),
        # Parsing info
        sa.Column("content_type", sa.String(50), nullable=False),
        sa.Column("parsed_from", sa.String(50), nullable=True),
        sa.Column("parser_version", sa.String(20), nullable=True),
        sa.Column("parse_metadata", sa.Text(), nullable=True),  # JSON
        # Version control
        sa.Column("backend_version", sa.String(255), nullable=True),
        # Freshness tracking
        sa.Column(
            "synced_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    # Create indexes
    op.create_index("idx_content_cache_tenant", "content_cache", ["tenant_id"])
    op.create_index("idx_content_cache_synced", "content_cache", ["synced_at"])

    # Partial index for stale entries (PostgreSQL only)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE INDEX idx_content_cache_stale
            ON content_cache (stale)
            WHERE stale = true
            """
        )
    else:
        # SQLite: regular index
        op.create_index("idx_content_cache_stale", "content_cache", ["stale"])


def downgrade() -> None:
    """Drop content_cache table."""
    # Drop indexes
    op.drop_index("idx_content_cache_stale", table_name="content_cache")
    op.drop_index("idx_content_cache_synced", table_name="content_cache")
    op.drop_index("idx_content_cache_tenant", table_name="content_cache")

    # Drop table
    op.drop_table("content_cache")
