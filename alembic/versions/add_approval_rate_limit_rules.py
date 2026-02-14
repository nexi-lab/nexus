"""Add spending_approvals table, rate limit columns, and rules column.

Issue #1358 Phases 2-4:
- Phase 2: spending_approvals table for approval workflows
- Phase 3: max_tx_per_hour, max_tx_per_day on spending_policies
- Phase 4: rules (JSON text) on spending_policies

Revision ID: add_approval_rate_limit_rules
Revises: add_spending_policy_tables
Create Date: 2026-02-13
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "add_approval_rate_limit_rules"
down_revision: Union[str, Sequence[str], None] = "add_spending_policy_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add Phase 2-4 schema changes."""
    # --- Phase 2: spending_approvals table ---
    op.create_table(
        "spending_approvals",
        sa.Column("id", sa.String(36), primary_key=True, nullable=False),
        sa.Column("policy_id", sa.String(36), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("zone_id", sa.String(255), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("to", sa.String(255), nullable=False),
        sa.Column("memo", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_spending_approvals_policy_id", "spending_approvals", ["policy_id"])
    op.create_index("ix_spending_approvals_agent_id", "spending_approvals", ["agent_id"])
    op.create_index(
        "ix_spending_approvals_agent_zone",
        "spending_approvals",
        ["agent_id", "zone_id"],
    )
    op.create_index("ix_spending_approvals_status", "spending_approvals", ["status"])

    # --- Phase 3: rate limit columns on spending_policies ---
    with op.batch_alter_table("spending_policies") as batch_op:
        batch_op.add_column(sa.Column("max_tx_per_hour", sa.Integer, nullable=True))
        batch_op.add_column(sa.Column("max_tx_per_day", sa.Integer, nullable=True))
        # --- Phase 4: rules JSON column ---
        batch_op.add_column(sa.Column("rules", sa.Text, nullable=True))


def downgrade() -> None:
    """Reverse Phase 2-4 schema changes."""
    with op.batch_alter_table("spending_policies") as batch_op:
        batch_op.drop_column("rules")
        batch_op.drop_column("max_tx_per_day")
        batch_op.drop_column("max_tx_per_hour")

    op.drop_table("spending_approvals")
