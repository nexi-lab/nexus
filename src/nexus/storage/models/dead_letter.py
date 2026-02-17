"""DeadLetterModel — dead letter queue for failed event exports.

Stores events that could not be delivered to external exporters after
exhausting retries, enabling manual inspection and replay.

Issue #1138: Event Stream Export.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base


class DeadLetterModel(Base):
    """Dead letter queue entry for failed event stream exports."""

    __tablename__ = "dead_letter_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    exporter_name: Mapped[str] = mapped_column(String(100), nullable=False)
    event_payload: Mapped[str] = mapped_column(Text, nullable=False)
    failure_type: Mapped[str] = mapped_column(String(20), nullable=False)  # transient | permanent
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    retry_count: Mapped[int] = mapped_column(nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_dlq_exporter_unresolved", "exporter_name", "created_at"),
        Index("idx_dlq_operation", "operation_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<DeadLetterModel(id={self.id}, op={self.operation_id}, "
            f"exporter={self.exporter_name}, type={self.failure_type})>"
        )
