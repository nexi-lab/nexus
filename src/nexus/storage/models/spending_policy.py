"""SQLAlchemy models for spending policies, ledger, and approvals.

Issue #1358: Agent Spending Policy Engine (Phases 1-4).

Tables:
    spending_policies — declarative budget limits per agent/zone
    spending_ledger   — period-based spending counters (atomic UPSERT)
    spending_approvals — approval workflow records (Phase 2)
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, uuid_pk


class SpendingPolicyModel(Base):
    """Declarative spending policy for an agent or zone.

    agent_id=NULL means zone-level default policy.
    All limit amounts stored as micro-credits (BigInteger) for precision.
    """

    __tablename__ = "spending_policies"

    id: Mapped[str] = uuid_pk()
    agent_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    daily_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    weekly_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    monthly_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    per_tx_limit: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    auto_approve_threshold: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Phase 3: rate controls
    max_tx_per_hour: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_tx_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Phase 4: policy DSL rules (JSON stored as text)
    rules: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
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
        UniqueConstraint("agent_id", "zone_id", name="uq_spending_policy_agent_zone"),
        Index("ix_spending_policies_zone_priority", "zone_id", "priority"),
    )


class SpendingLedgerModel(Base):
    """Period-based spending counter.

    Updated atomically via UPSERT on each successful transfer.
    One row per (agent_id, zone_id, period_type, period_start).
    All amounts in micro-credits (BigInteger).
    """

    __tablename__ = "spending_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # daily|weekly|monthly
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    amount_spent: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    tx_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    __table_args__ = (
        UniqueConstraint(
            "agent_id",
            "zone_id",
            "period_type",
            "period_start",
            name="uq_spending_ledger_agent_period",
        ),
        Index("ix_spending_ledger_agent_zone", "agent_id", "zone_id"),
    )


class SpendingApprovalModel(Base):
    """Approval workflow record (Phase 2).

    Created when a transaction exceeds auto_approve_threshold.
    Admin approves/rejects via API. Agent retries with approval_id.
    """

    __tablename__ = "spending_approvals"

    id: Mapped[str] = uuid_pk()
    policy_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)  # micro-credits
    to: Mapped[str] = mapped_column(String(255), nullable=False)
    memo: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )  # pending|approved|rejected|expired
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_spending_approvals_agent_zone", "agent_id", "zone_id"),
        Index("ix_spending_approvals_status", "status"),
    )
