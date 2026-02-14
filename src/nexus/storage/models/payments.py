"""Payment, wallet, and usage metering models (Nexus Pay).

Issue #1286: Extracted from monolithic __init__.py.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, uuid_pk


class AgentWalletMeta(Base):
    """Wallet metadata for Nexus Pay. Balances in TigerBeetle, settings here.

    Note: Budget limits moved to spending_policies table (Issue #1358).
    """

    __tablename__ = "agent_wallet_meta"

    agent_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_account_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    x402_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    x402_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        Index("idx_wallet_meta_zone", "zone_id"),
        Index("idx_wallet_meta_tb_id", "tigerbeetle_account_id"),
    )


class PaymentTransactionMeta(Base):
    """Transaction metadata for Nexus Pay. Amounts in TigerBeetle, context here."""

    __tablename__ = "payment_transaction_meta"

    id: Mapped[str] = uuid_pk()
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_transfer_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    from_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    to_agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="credits")
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    memo: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    x402_tx_hash: Mapped[str | None] = mapped_column(String(66), nullable=True)
    x402_network: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="completed")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_tx_meta_from_time", "from_agent_id", "created_at"),
        Index("idx_tx_meta_to_time", "to_agent_id", "created_at"),
        Index("idx_tx_meta_zone_time", "zone_id", "created_at"),
        Index("idx_tx_meta_task", "task_id"),
        Index("idx_tx_meta_x402_hash", "x402_tx_hash"),
    )


class CreditReservationMeta(Base):
    """Credit reservation metadata for two-phase transfers."""

    __tablename__ = "credit_reservation_meta"

    id: Mapped[str] = uuid_pk()
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    tigerbeetle_transfer_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    purpose: Mapped[str] = mapped_column(String(50), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    queue_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_reservation_agent_status", "agent_id", "status"),
        Index("idx_reservation_expires", "expires_at"),
        Index("idx_reservation_task", "task_id"),
        Index("idx_reservation_zone", "zone_id", "status"),
    )


class UsageEvent(Base):
    """Usage events for API metering and analytics."""

    __tablename__ = "usage_events"

    id: Mapped[str] = uuid_pk()
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resource: Mapped[str | None] = mapped_column(String(200), nullable=True)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_usage_zone_type_time", "zone_id", "event_type", "created_at"),
        Index("idx_usage_agent_time", "agent_id", "created_at"),
        Index("idx_usage_resource", "resource"),
    )

    def get_metadata(self) -> dict[str, Any]:
        """Parse metadata JSON."""
        result: dict[str, Any] = json.loads(self.metadata_json) if self.metadata_json else {}
        return result

    def set_metadata(self, data: dict[str, Any]) -> None:
        """Serialize metadata to JSON."""
        self.metadata_json = json.dumps(data) if data else None
