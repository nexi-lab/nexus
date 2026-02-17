"""Add transaction_snapshots table (Issue #1752).

Creates the transaction_snapshots table for atomic COW filesystem snapshots
used by agent rollback. Each row = one transaction lifecycle
(ACTIVE -> COMMITTED | ROLLED_BACK | EXPIRED).

Revision ID: add_transaction_snapshots
Revises: add_seq_number_dlq
Create Date: 2026-02-17
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_transaction_snapshots"
down_revision: Union[str, Sequence[str], None] = "add_seq_number_dlq"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add transaction_snapshots table with indexes."""
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.create_table(
        "transaction_snapshots",
        sa.Column("snapshot_id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(255), nullable=False),
        sa.Column("zone_id", sa.String(36), nullable=False, server_default="root"),
        sa.Column("status", sa.String(20), nullable=False, server_default="ACTIVE"),
        sa.Column("paths_json", sa.Text(), nullable=False),
        sa.Column("snapshot_data_json", sa.Text(), nullable=False),
        sa.Column("path_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("committed_at", sa.DateTime(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(), nullable=True),
    )

    # Active transactions per agent (most common lookup)
    op.create_index(
        "idx_txn_snapshot_agent_status",
        "transaction_snapshots",
        ["agent_id", "status"],
    )

    # TTL cleanup: partial index on ACTIVE only (PostgreSQL)
    if dialect == "postgresql":
        op.create_index(
            "idx_txn_snapshot_active_expiry",
            "transaction_snapshots",
            ["expires_at"],
            postgresql_where=sa.text("status = 'ACTIVE'"),
        )
    else:
        # SQLite doesn't support partial indexes — full index
        op.create_index(
            "idx_txn_snapshot_active_expiry",
            "transaction_snapshots",
            ["expires_at"],
        )

    # Zone-scoped queries
    op.create_index(
        "idx_txn_snapshot_zone_agent",
        "transaction_snapshots",
        ["zone_id", "agent_id"],
    )


def downgrade() -> None:
    """Remove transaction_snapshots table."""
    op.drop_index("idx_txn_snapshot_zone_agent", table_name="transaction_snapshots")
    op.drop_index("idx_txn_snapshot_active_expiry", table_name="transaction_snapshots")
    op.drop_index("idx_txn_snapshot_agent_status", table_name="transaction_snapshots")
    op.drop_table("transaction_snapshots")
