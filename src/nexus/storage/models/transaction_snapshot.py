"""Transaction snapshot models for agent filesystem rollback (Issue #1752).

Two-table design:
- TransactionSnapshotModel: parent record (one per transaction)
- SnapshotEntryModel: per-file record (one per tracked path within a transaction)

Follows patterns from operation_log.py.
"""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class TransactionSnapshotModel(Base):
    """Parent record for a transactional filesystem snapshot.

    Status lifecycle: creating -> active -> committed | rolled_back | expired
    """

    __tablename__ = "transaction_snapshot"

    transaction_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default=ROOT_ZONE_ID)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    entry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (
        Index("idx_txn_snapshot_zone", "zone_id"),
        Index("idx_txn_snapshot_status", "status"),
        Index("idx_txn_snapshot_expires", "expires_at"),
        Index(
            "idx_txn_snapshot_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
        # Partial index for cleanup worker: only active transactions past expiry
        Index(
            "idx_txn_snapshot_active_expires",
            "expires_at",
            postgresql_where=text("status = 'active'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<TransactionSnapshotModel("
            f"transaction_id={self.transaction_id}, "
            f"status={self.status}, "
            f"entry_count={self.entry_count})>"
        )


class SnapshotEntryModel(Base):
    """Per-file record within a transactional snapshot.

    Captures the original state of a file before modification,
    enabling rollback to the pre-transaction state.
    """

    __tablename__ = "snapshot_entry"

    entry_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )
    transaction_id: Mapped[str] = mapped_column(String(36), nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    operation: Mapped[str] = mapped_column(String(20), nullable=False)
    original_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_metadata: Mapped[str | None] = mapped_column(Text, nullable=True)
    new_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        Index("idx_snapshot_entry_txn", "transaction_id"),
        Index("idx_snapshot_entry_txn_path", "transaction_id", "path"),
    )

    def __repr__(self) -> str:
        return (
            f"<SnapshotEntryModel("
            f"entry_id={self.entry_id}, "
            f"transaction_id={self.transaction_id}, "
            f"path={self.path}, "
            f"operation={self.operation})>"
        )
