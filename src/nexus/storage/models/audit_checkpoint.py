"""AuditCheckpointModel — periodic Merkle root checkpoints.

Issue #1360 Phase 1: Stores Merkle roots computed over ranges of
exchange audit log records for batch integrity verification.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class AuditCheckpointModel(Base):
    """Periodic Merkle root checkpoint over audit log records."""

    __tablename__ = "audit_checkpoint"

    id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    checkpoint_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False)
    merkle_root: Mapped[str] = mapped_column(String(64), nullable=False)

    first_record_id: Mapped[str] = mapped_column(String(36), nullable=False)
    last_record_id: Mapped[str] = mapped_column(String(36), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditCheckpoint(id={self.id}, records={self.record_count}, "
            f"root={self.merkle_root[:12]}...)>"
        )
