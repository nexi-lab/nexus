"""Add spending_policies and spending_ledger tables (Issue #1358).

Phase 1 of Agent Spending Policy Engine:
- spending_policies: declarative budget limits per agent/zone
- spending_ledger: period-based spending counters (atomic UPSERT)
- Drops unused budget columns from agent_wallet_meta

Revision ID: add_spending_policy_tables
Revises: merge_agent_keys_ns_views
Create Date: 2026-02-13
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_spending_policy_tables"
down_revision: Union[str, Sequence[str], None] = "merge_agent_keys_ns_views"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create spending policy tables and clean up dead columns."""
    # --- spending_policies table ---
    op.create_table(
        "spending_policies",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("daily_limit", sa.BigInteger, nullable=True),
        sa.Column("weekly_limit", sa.BigInteger, nullable=True),
        sa.Column("monthly_limit", sa.BigInteger, nullable=True),
        sa.Column("per_tx_limit", sa.BigInteger, nullable=True),
        sa.Column("auto_approve_threshold", sa.BigInteger, nullable=True),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_spending_policies_agent_id", "spending_policies", ["agent_id"])
    op.create_index("ix_spending_policies_zone_id", "spending_policies", ["zone_id"])
    op.create_index(
        "ix_spending_policies_zone_priority",
        "spending_policies",
        ["zone_id", "priority"],
    )
    op.create_unique_constraint(
        "uq_spending_policy_agent_zone",
        "spending_policies",
        ["agent_id", "zone_id"],
    )

    # --- spending_ledger table ---
    op.create_table(
        "spending_ledger",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("period_type", sa.String(10), nullable=False),
        sa.Column("period_start", sa.Date, nullable=False),
        sa.Column("amount_spent", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("tx_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_spending_ledger_agent_zone",
        "spending_ledger",
        ["agent_id", "zone_id"],
    )
    op.create_unique_constraint(
        "uq_spending_ledger_agent_period",
        "spending_ledger",
        ["agent_id", "zone_id", "period_type", "period_start"],
    )

    # --- Drop unused columns from agent_wallet_meta ---
    # These columns were added but never referenced by any code.
    with op.batch_alter_table("agent_wallet_meta") as batch_op:
        batch_op.drop_index("idx_wallet_meta_daily_reset")
        batch_op.drop_index("idx_wallet_meta_monthly_reset")
        batch_op.drop_column("daily_limit")
        batch_op.drop_column("monthly_limit")
        batch_op.drop_column("per_tx_limit")
        batch_op.drop_column("daily_spent")
        batch_op.drop_column("monthly_spent")
        batch_op.drop_column("daily_reset_at")
        batch_op.drop_column("monthly_reset_at")


def downgrade() -> None:
    """Reverse: restore columns, drop tables."""
    # Restore agent_wallet_meta columns
    with op.batch_alter_table("agent_wallet_meta") as batch_op:
        batch_op.add_column(sa.Column("daily_limit", sa.BigInteger, nullable=True))
        batch_op.add_column(sa.Column("monthly_limit", sa.BigInteger, nullable=True))
        batch_op.add_column(sa.Column("per_tx_limit", sa.BigInteger, nullable=True))
        batch_op.add_column(
            sa.Column("daily_spent", sa.BigInteger, nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("monthly_spent", sa.BigInteger, nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column(
                "daily_reset_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "monthly_reset_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            )
        )
        batch_op.create_index("idx_wallet_meta_daily_reset", ["daily_reset_at"])
        batch_op.create_index("idx_wallet_meta_monthly_reset", ["monthly_reset_at"])

    op.drop_table("spending_ledger")
    op.drop_table("spending_policies")
