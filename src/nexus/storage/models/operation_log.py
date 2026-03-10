"""OperationLogModel — audit trail for filesystem operations.

Issue #1246 Phase 4: Extracted from monolithic models.py.
"""

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Index, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class OperationLogModel(Base):
    """Operation log for tracking filesystem operations.

    Provides audit trail, undo capability, and debugging support.
    Stores snapshots of state before operations for rollback.
    """

    __tablename__ = "operation_log"

    # Primary key
    operation_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Operation identification
    operation_type: Mapped[str] = mapped_column(String(50), nullable=False)

    # Context
    zone_id: Mapped[str] = mapped_column(String(36), nullable=False, default=ROOT_ZONE_ID)
    agent_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Affected paths
    path: Mapped[str] = mapped_column(Text, nullable=False)
    new_path: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Snapshot data (CAS-backed)
    snapshot_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Metadata snapshot (JSON)
    metadata_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Operation result
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Transactional outbox (Issue #1241): tracks whether event has been
    # delivered to downstream systems (EventBus, webhooks, hooks).
    delivered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Monotonic sequence for cursor-based replay pagination (Issue #1138/#1139).
    # Nullable for backfill of existing rows; new rows auto-populated via trigger/app.
    sequence_number: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, default=None, unique=True
    )

    # Persistent retry count for delivery worker (Issue #2751).
    # Survives worker restarts; prevents over-retry and DLQ bypass.
    retry_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    # Indexes
    __table_args__ = (
        Index("idx_operation_log_type", "operation_type"),
        Index("idx_operation_log_agent", "agent_id"),
        Index("idx_operation_log_zone", "zone_id"),
        Index("idx_operation_log_path", "path"),
        Index("idx_operation_log_created_at", "created_at"),
        Index("idx_operation_log_status", "status"),
        Index(
            "idx_operation_log_created_brin",
            "created_at",
            postgresql_using="brin",
        ),
        Index(
            "idx_operation_log_zone_created_brin",
            "zone_id",
            "created_at",
            postgresql_using="brin",
        ),
        # Composite B-tree for agent activity summary queries (#1198).
        # Covers: WHERE zone_id=? AND agent_id=? AND created_at>=?
        # + GROUP BY operation_type / GROUP BY path ORDER BY created_at DESC
        Index(
            "idx_operation_log_zone_agent_created",
            "zone_id",
            "agent_id",
            created_at.desc(),
        ),
        # Transactional outbox index (Issue #1241): partial index on
        # undelivered rows for efficient polling by EventDeliveryWorker.
        # PostgreSQL-only; SQLite migration uses a regular index fallback.
        Index(
            "idx_operation_log_undelivered",
            "created_at",
            postgresql_where=text("delivered = false"),
        ),
        # BRIN index for efficient replay cursor queries (Issue #1138/#1139).
        Index(
            "idx_operation_log_zone_seq_brin",
            "zone_id",
            "sequence_number",
            postgresql_using="brin",
        ),
    )

    def __repr__(self) -> str:
        return f"<OperationLogModel(operation_id={self.operation_id}, type={self.operation_type}, path={self.path})>"

    def validate(self) -> None:
        """Validate operation log model before database operations."""
        from nexus.contracts.exceptions import ValidationError
        from nexus.contracts.operation_types import OperationType

        valid_types = [t.value for t in OperationType]
        if self.operation_type not in valid_types:
            raise ValidationError(
                f"operation_type must be one of {valid_types}, got {self.operation_type}"
            )

        if not self.path:
            raise ValidationError("path is required")

        valid_statuses = ["success", "failure"]
        if self.status not in valid_statuses:
            raise ValidationError(f"status must be one of {valid_statuses}, got {self.status}")
