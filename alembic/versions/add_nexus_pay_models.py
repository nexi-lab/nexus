"""Add Nexus Pay database models

Issue #1199: Add database models for Nexus Pay hybrid architecture.

Creates tables for:
- agent_wallet_meta: Wallet settings and budget tracking
- payment_transaction_meta: Transaction context and memos
- credit_reservation_meta: Two-phase transfer context
- usage_events: API metering and analytics

Revision ID: add_nexus_pay_models
Revises: add_bitemporal_validity
Create Date: 2026-02-05

"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "add_nexus_pay_models"
down_revision: Union[str, Sequence[str], None] = "add_bitemporal_validity"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create Nexus Pay tables and indexes."""
    bind = op.get_bind()

    # AgentWalletMeta
    op.create_table(
        "agent_wallet_meta",
        sa.Column("agent_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("tigerbeetle_account_id", sa.BigInteger, unique=True, nullable=False),
        sa.Column("x402_address", sa.String(64), nullable=True),
        sa.Column("x402_enabled", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("daily_limit", sa.BigInteger, nullable=True),
        sa.Column("monthly_limit", sa.BigInteger, nullable=True),
        sa.Column("per_tx_limit", sa.BigInteger, nullable=True),
        sa.Column("daily_spent", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("monthly_spent", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "daily_reset_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "monthly_reset_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
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
    op.create_index("idx_wallet_meta_tenant", "agent_wallet_meta", ["tenant_id"])
    op.create_index("idx_wallet_meta_tb_id", "agent_wallet_meta", ["tigerbeetle_account_id"])
    op.create_index("idx_wallet_meta_daily_reset", "agent_wallet_meta", ["daily_reset_at"])
    op.create_index("idx_wallet_meta_monthly_reset", "agent_wallet_meta", ["monthly_reset_at"])

    # PaymentTransactionMeta
    op.create_table(
        "payment_transaction_meta",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("tigerbeetle_transfer_id", sa.BigInteger, unique=True, nullable=False),
        sa.Column("from_agent_id", sa.String(64), nullable=False),
        sa.Column("to_agent_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("currency", sa.String(10), nullable=False, server_default="credits"),
        sa.Column("method", sa.String(20), nullable=False),
        sa.Column("memo", sa.Text, nullable=True),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("x402_tx_hash", sa.String(66), nullable=True),
        sa.Column("x402_network", sa.String(20), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_tx_meta_from_time", "payment_transaction_meta", ["from_agent_id", "created_at"]
    )
    op.create_index(
        "idx_tx_meta_to_time", "payment_transaction_meta", ["to_agent_id", "created_at"]
    )
    op.create_index(
        "idx_tx_meta_tenant_time", "payment_transaction_meta", ["tenant_id", "created_at"]
    )
    op.create_index("idx_tx_meta_task", "payment_transaction_meta", ["task_id"])
    op.create_index("idx_tx_meta_x402_hash", "payment_transaction_meta", ["x402_tx_hash"])

    # CreditReservationMeta
    op.create_table(
        "credit_reservation_meta",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("tigerbeetle_transfer_id", sa.BigInteger, unique=True, nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("task_id", sa.String(36), nullable=True),
        sa.Column("queue_name", sa.String(100), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_reservation_agent_status", "credit_reservation_meta", ["agent_id", "status"]
    )
    op.create_index("idx_reservation_expires", "credit_reservation_meta", ["expires_at"])
    op.create_index("idx_reservation_task", "credit_reservation_meta", ["task_id"])
    op.create_index("idx_reservation_tenant", "credit_reservation_meta", ["tenant_id", "status"])

    # UsageEvent
    op.create_table(
        "usage_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("tenant_id", sa.String(255), nullable=False, server_default="default"),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("amount", sa.BigInteger, nullable=False),
        sa.Column("resource", sa.String(200), nullable=True),
        sa.Column("metadata_json", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_usage_tenant_type_time",
        "usage_events",
        ["tenant_id", "event_type", "created_at"],
    )
    op.create_index("idx_usage_agent_time", "usage_events", ["agent_id", "created_at"])
    op.create_index("idx_usage_resource", "usage_events", ["resource"])

    # PostgreSQL BRIN index for time-series
    if bind.dialect.name == "postgresql":
        op.create_index(
            "idx_usage_created_at_brin",
            "usage_events",
            ["created_at"],
            postgresql_using="brin",
        )


def downgrade() -> None:
    """Drop Nexus Pay tables."""
    bind = op.get_bind()

    if bind.dialect.name == "postgresql":
        op.drop_index("idx_usage_created_at_brin", table_name="usage_events")

    op.drop_index("idx_usage_resource", table_name="usage_events")
    op.drop_index("idx_usage_agent_time", table_name="usage_events")
    op.drop_index("idx_usage_tenant_type_time", table_name="usage_events")
    op.drop_table("usage_events")

    op.drop_index("idx_reservation_tenant", table_name="credit_reservation_meta")
    op.drop_index("idx_reservation_task", table_name="credit_reservation_meta")
    op.drop_index("idx_reservation_expires", table_name="credit_reservation_meta")
    op.drop_index("idx_reservation_agent_status", table_name="credit_reservation_meta")
    op.drop_table("credit_reservation_meta")

    op.drop_index("idx_tx_meta_x402_hash", table_name="payment_transaction_meta")
    op.drop_index("idx_tx_meta_task", table_name="payment_transaction_meta")
    op.drop_index("idx_tx_meta_tenant_time", table_name="payment_transaction_meta")
    op.drop_index("idx_tx_meta_to_time", table_name="payment_transaction_meta")
    op.drop_index("idx_tx_meta_from_time", table_name="payment_transaction_meta")
    op.drop_table("payment_transaction_meta")

    op.drop_index("idx_wallet_meta_monthly_reset", table_name="agent_wallet_meta")
    op.drop_index("idx_wallet_meta_daily_reset", table_name="agent_wallet_meta")
    op.drop_index("idx_wallet_meta_tb_id", table_name="agent_wallet_meta")
    op.drop_index("idx_wallet_meta_tenant", table_name="agent_wallet_meta")
    op.drop_table("agent_wallet_meta")
