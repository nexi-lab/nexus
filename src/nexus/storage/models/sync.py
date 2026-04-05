"""Sync and conflict resolution models.

Issue #1286: Extracted from monolithic __init__.py.
"""

import json
from datetime import UTC, datetime

from sqlalchemy import BigInteger, DateTime, Float, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.contracts.exceptions import ValidationError
from nexus.storage.models._base import Base, uuid_pk


class SyncJobModel(Base):
    """Async sync job tracking for long-running mount synchronization."""

    __tablename__ = "sync_jobs"

    id: Mapped[str] = uuid_pk()

    mount_point: Mapped[str] = mapped_column(Text, nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")

    progress_pct: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    progress_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    sync_params: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)

    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("idx_sync_jobs_mount_point", "mount_point"),
        Index("idx_sync_jobs_status", "status"),
        Index("idx_sync_jobs_created_at", "created_at"),
        Index("idx_sync_jobs_created_by", "created_by"),
    )

    def __repr__(self) -> str:
        return f"<SyncJobModel(id={self.id}, mount_point={self.mount_point}, status={self.status}, progress={self.progress_pct}%)>"

    def to_dict(self) -> dict:
        """Convert to dictionary for API responses."""
        return {
            "id": self.id,
            "mount_point": self.mount_point,
            "status": self.status,
            "progress_pct": self.progress_pct,
            "progress_detail": json.loads(self.progress_detail) if self.progress_detail else None,
            "sync_params": json.loads(self.sync_params) if self.sync_params else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "created_by": self.created_by,
            "result": json.loads(self.result) if self.result else None,
            "error_message": self.error_message,
        }


class BackendChangeLogModel(Base):
    """Change log for delta sync tracking (Issue #1127).

    Tracks the last synced state of each file per backend, enabling
    incremental sync by comparing against current backend state.
    """

    __tablename__ = "backend_change_log"

    id: Mapped[str] = uuid_pk()

    path: Mapped[str] = mapped_column(String(4096), nullable=False)
    backend_name: Mapped[str] = mapped_column(String(255), nullable=False)

    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    mtime: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    backend_version: Mapped[str | None] = mapped_column(String(255), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    synced_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)

    __table_args__ = (
        UniqueConstraint("path", "backend_name", "zone_id", name="uq_backend_change_log"),
        Index("idx_bcl_path_backend", "path", "backend_name"),
        Index("idx_bcl_synced_at", "backend_name", "synced_at"),
        Index("idx_bcl_zone", "zone_id"),
        Index("idx_bcl_synced_brin", "synced_at", postgresql_using="brin"),
    )

    def __repr__(self) -> str:
        return f"<BackendChangeLogModel(path={self.path}, backend={self.backend_name}, synced_at={self.synced_at})>"

    def validate(self) -> None:
        """Validate change log model before database operations."""
        if not self.path:
            raise ValidationError("path is required")
        if not self.backend_name:
            raise ValidationError("backend_name is required")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValidationError(f"size_bytes cannot be negative, got {self.size_bytes}")


class PendingOperationModel(Base):
    """Offline queue pending operation for proxy replay.

    Replaces raw SQLite DDL in proxy/offline_queue.py with a proper ORM model.
    """

    __tablename__ = "pending_ops"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    method: Mapped[str] = mapped_column(Text, nullable=False)
    args_json: Mapped[str] = mapped_column(Text, nullable=False)
    kwargs_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_ref: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    vector_clock: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("idx_pending_ops_status", "status", "id"),)
