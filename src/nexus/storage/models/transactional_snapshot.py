"""TransactionSnapshotModel — transactional filesystem snapshots for agent rollback.

Issue #1752: Stores snapshot state for atomic COW operations.
Each row represents one transaction lifecycle (ACTIVE -> COMMITTED/ROLLED_BACK/EXPIRED).

Design:
    - paths_json: JSON array of virtual paths included in snapshot
    - snapshot_data_json: JSON object mapping path -> {content_hash, size, metadata, existed}
    - Indexes optimized for: active lookup, TTL cleanup, zone-scoped queries
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class TransactionSnapshotModel(Base):
    """Transactional filesystem snapshot for agent rollback.

    Strict state machine: ACTIVE -> COMMITTED | ROLLED_BACK | EXPIRED.
    No backward transitions allowed.
    """

    __tablename__ = "transaction_snapshots"

    # Primary key
    snapshot_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Transaction context
    agent_id: Mapped[str] = mapped_column(String(255), nullable=False)
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default="root")

    # Lifecycle state
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="ACTIVE")

    # Snapshot data
    paths_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_data_json: Mapped[str] = mapped_column(Text, nullable=False)

    # Path count (denormalized for queries)
    path_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    committed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes
    __table_args__ = (
        # Active transactions per agent (most common lookup)
        Index("idx_txn_snapshot_agent_status", "agent_id", "status"),
        # TTL cleanup: partial index on ACTIVE only (PostgreSQL)
        Index(
            "idx_txn_snapshot_active_expiry",
            "expires_at",
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        # Zone-scoped queries
        Index("idx_txn_snapshot_zone_agent", "zone_id", "agent_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<TransactionSnapshotModel("
            f"snapshot_id={self.snapshot_id}, "
            f"agent_id={self.agent_id}, "
            f"status={self.status}, "
            f"path_count={self.path_count})>"
        )

    def validate(self) -> None:
        """Validate model before database operations."""
        from nexus.core.exceptions import ValidationError

        valid_statuses = {"ACTIVE", "COMMITTED", "ROLLED_BACK", "EXPIRED"}
        if self.status not in valid_statuses:
            raise ValidationError(
                f"status must be one of {valid_statuses}, got {self.status}"
            )

        if not self.agent_id:
            raise ValidationError("agent_id is required")

        if not self.zone_id:
            raise ValidationError("zone_id is required")

        if not self.paths_json:
            raise ValidationError("paths_json is required")

        if not self.snapshot_data_json:
            raise ValidationError("snapshot_data_json is required")

        if self.path_count < 0:
            raise ValidationError(f"path_count cannot be negative, got {self.path_count}")
