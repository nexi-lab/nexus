"""ExchangeAuditLogModel — immutable transaction audit trail.

Issue #1360 Phase 1: Every exchange transaction is recorded with full
provenance for compliance, dispute resolution, and analytics.

Design decisions:
- Composite PK (id, created_at) for future time-based partitioning.
- SHA-256 self-hash per record for tamper detection.
- String columns for enums (no PG ENUM) for forward compatibility.
- 5 strategic indexes (2 composite B-tree, 2 BRIN, 1 unique).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class ExchangeAuditLogModel(Base):
    """Immutable audit log for exchange transactions.

    Records are append-only: updates and deletes are rejected by both
    an ORM event guard and a PostgreSQL trigger.
    """

    __tablename__ = "exchange_audit_log"

    # Composite primary key — partition-ready (Decision #16)
    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    # Self-hash for tamper detection (SHA-256 hex digest)
    record_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # Transaction identification
    protocol: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    application: Mapped[str] = mapped_column(String(20), nullable=False)

    # Counterparties
    buyer_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    seller_agent_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Financial
    amount: Mapped[str] = mapped_column(Numeric(18, 6), nullable=False)
    currency: Mapped[str] = mapped_column(String(10), nullable=False, default="credits")

    # Context
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default="default")
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transfer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Indexes (Decision #14)
    __table_args__ = (
        # 1. Composite B-tree: buyer lookups by time
        Index("idx_exchange_audit_buyer_created", "buyer_agent_id", "created_at"),
        # 2. Composite B-tree: seller lookups by time
        Index("idx_exchange_audit_seller_created", "seller_agent_id", "created_at"),
        # 3. BRIN: time-range scans (append-only data is perfectly correlated)
        Index(
            "idx_exchange_audit_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
        # 4. Composite BRIN: zone + time scans
        Index(
            "idx_exchange_audit_zone_created_brin",
            "zone_id",
            "created_at",
            postgresql_using="brin",
        ),
        # 5. Unique: OTEL trace correlation
        Index("idx_exchange_audit_trace_id", "trace_id", unique=True),
    )

    def __repr__(self) -> str:
        return (
            f"<ExchangeAuditLog(id={self.id}, protocol={self.protocol}, "
            f"amount={self.amount}, status={self.status})>"
        )
