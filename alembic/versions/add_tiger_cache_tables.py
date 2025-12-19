"""Add Tiger Cache tables for materialized permissions

Revision ID: add_tiger_cache
Revises: add_leopard_closure
Create Date: 2025-12-19

Adds tables for Tiger Cache - pre-materialized permissions stored as Roaring Bitmaps
for O(1) list operations.

Related to: #682 (Tiger Cache for materialized permissions)

Tables:
- tiger_resource_map: Maps resource UUIDs to int64 IDs for bitmap storage
- tiger_cache: Stores serialized bitmaps per (subject, permission, tenant)

Performance Impact:
- List operations: O(n) -> O(1)
- 10-100x speedup for directory listings
- Background updates don't block reads
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_tiger_cache"
down_revision: Union[str, Sequence[str], None] = "add_leopard_closure"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create Tiger Cache tables."""
    bind = op.get_bind()

    # 1. Resource mapping table: UUID -> int64 for Roaring Bitmap compatibility
    op.create_table(
        "tiger_resource_map",
        sa.Column("resource_int_id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("resource_type", sa.String(50), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Unique constraint on (resource_type, resource_id, tenant_id)
        sa.UniqueConstraint("resource_type", "resource_id", "tenant_id", name="uq_tiger_resource"),
    )

    # Index for reverse lookup (int64 -> UUID)
    op.create_index(
        "idx_tiger_resource_lookup",
        "tiger_resource_map",
        ["tenant_id", "resource_type", "resource_id"],
    )

    # 2. Tiger Cache table: stores serialized bitmaps
    op.create_table(
        "tiger_cache",
        sa.Column("cache_id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Subject (who has access)
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        # Permission type (read, write, execute, etc.)
        sa.Column("permission", sa.String(50), nullable=False),
        # Resource type (file, directory, etc.)
        sa.Column("resource_type", sa.String(50), nullable=False),
        # Tenant isolation
        sa.Column("tenant_id", sa.String(255), nullable=False),
        # Serialized Roaring Bitmap (binary)
        sa.Column("bitmap_data", sa.LargeBinary, nullable=False),
        # Revision for staleness detection
        sa.Column("revision", sa.BigInteger, nullable=False, default=0),
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        # Unique constraint
        sa.UniqueConstraint(
            "subject_type",
            "subject_id",
            "permission",
            "resource_type",
            "tenant_id",
            name="uq_tiger_cache",
        ),
    )

    # Index for fast cache lookup
    op.create_index(
        "idx_tiger_cache_lookup",
        "tiger_cache",
        ["tenant_id", "subject_type", "subject_id", "permission", "resource_type"],
    )

    # Index for revision-based invalidation
    op.create_index(
        "idx_tiger_cache_revision",
        "tiger_cache",
        ["revision"],
    )

    # 3. Tiger Cache update queue (for async background updates)
    op.create_table(
        "tiger_cache_queue",
        sa.Column("queue_id", sa.BigInteger, primary_key=True, autoincrement=True),
        # Subject to update
        sa.Column("subject_type", sa.String(50), nullable=False),
        sa.Column("subject_id", sa.String(255), nullable=False),
        # Permission to recompute
        sa.Column("permission", sa.String(50), nullable=False),
        # Resource type
        sa.Column("resource_type", sa.String(50), nullable=False),
        # Tenant
        sa.Column("tenant_id", sa.String(255), nullable=False),
        # Priority (lower = higher priority)
        sa.Column("priority", sa.Integer, nullable=False, default=100),
        # Status
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            default="pending",
        ),  # pending, processing, completed, failed
        # Timestamps
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        # Error info if failed
        sa.Column("error_message", sa.Text, nullable=True),
    )

    # Index for queue processing
    op.create_index(
        "idx_tiger_queue_pending",
        "tiger_cache_queue",
        ["status", "priority", "created_at"],
    )

    if bind.dialect.name == "postgresql":
        # PostgreSQL: Add BRIN index on created_at for efficient time-range queries
        op.execute(
            """
            CREATE INDEX idx_tiger_queue_created_brin
            ON tiger_cache_queue USING BRIN (created_at)
            """
        )


def downgrade() -> None:
    """Drop Tiger Cache tables."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_tiger_queue_created_brin")

    op.drop_index("idx_tiger_queue_pending", table_name="tiger_cache_queue")
    op.drop_table("tiger_cache_queue")

    op.drop_index("idx_tiger_cache_revision", table_name="tiger_cache")
    op.drop_index("idx_tiger_cache_lookup", table_name="tiger_cache")
    op.drop_table("tiger_cache")

    op.drop_index("idx_tiger_resource_lookup", table_name="tiger_resource_map")
    op.drop_table("tiger_resource_map")
