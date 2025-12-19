"""Add Leopard-style transitive group closure table

Revision ID: add_leopard_closure
Revises: add_backend_version_idx
Create Date: 2025-12-19

Adds rebac_group_closure table for pre-computed transitive group memberships.
This implements Leopard-style indexing from Google Zanzibar for O(1) group lookups.

Related to: #692 (Leopard-style pre-computed group membership index)

Performance Impact:
- 5-level nested group check: ~50ms -> ~1ms (50x faster)
- 10-level nested group check: ~200ms -> ~1ms (200x faster)
- Write latency: 2-5x slower (closure maintenance)
- Storage: O(members x groups)
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_leopard_closure"
down_revision: Union[str, Sequence[str], None] = "add_backend_version_idx"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create rebac_group_closure table for transitive group memberships."""
    bind = op.get_bind()

    # Create the closure table
    op.create_table(
        "rebac_group_closure",
        # Composite primary key
        sa.Column("member_type", sa.String(50), nullable=False),
        sa.Column("member_id", sa.String(255), nullable=False),
        sa.Column("group_type", sa.String(50), nullable=False),
        sa.Column("group_id", sa.String(255), nullable=False),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        # Metadata
        sa.Column("depth", sa.Integer, nullable=False),  # Distance in hierarchy
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Primary key
        sa.PrimaryKeyConstraint("member_type", "member_id", "group_type", "group_id", "tenant_id"),
    )

    # Create indexes for fast lookups
    # 1. Member lookup: "What groups does user:alice belong to?"
    op.create_index(
        "idx_closure_member",
        "rebac_group_closure",
        ["tenant_id", "member_type", "member_id"],
    )

    # 2. Group lookup: "Who are all members of group:engineering?"
    op.create_index(
        "idx_closure_group",
        "rebac_group_closure",
        ["tenant_id", "group_type", "group_id"],
    )

    # 3. Depth index for debugging/analytics
    op.create_index(
        "idx_closure_depth",
        "rebac_group_closure",
        ["depth"],
    )

    if bind.dialect.name == "postgresql":
        # PostgreSQL: Add BRIN index on updated_at for efficient time-range queries
        op.execute(
            """
            CREATE INDEX idx_closure_updated_at_brin
            ON rebac_group_closure USING BRIN (updated_at)
            """
        )


def downgrade() -> None:
    """Drop rebac_group_closure table."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS idx_closure_updated_at_brin")

    op.drop_index("idx_closure_depth", table_name="rebac_group_closure")
    op.drop_index("idx_closure_group", table_name="rebac_group_closure")
    op.drop_index("idx_closure_member", table_name="rebac_group_closure")
    op.drop_table("rebac_group_closure")
