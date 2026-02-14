"""IPCMessageModel — IPC message persistence via RecordStoreABC.

Replaces the raw asyncpg DDL that was previously inlined in
``PostgreSQLStorageDriver.initialize()``.  Now managed by Alembic
and accessed through the standard SQLAlchemy session factory.

Issue: #1469
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Index, LargeBinary, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, uuid_pk


class IPCMessageModel(Base):
    """IPC message or directory marker stored in PostgreSQL.

    Each row represents either a file (message payload) or a directory
    marker used for ``list_dir`` / ``count_dir`` operations.

    Columns mirror the original raw DDL from ``PostgreSQLStorageDriver``
    (Issue #1243) but are now managed by Alembic migrations.
    """

    __tablename__ = "ipc_messages"

    # Primary key
    id: Mapped[str] = uuid_pk()

    # Zone isolation
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False)

    # Path decomposition
    path: Mapped[str] = mapped_column(Text, nullable=False)
    dir_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)

    # Payload
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, default=b"")

    # Directory flag
    is_dir: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Timestamp
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )

    # Indexes
    __table_args__ = (
        # Unique per zone+path (enforces upsert semantics)
        Index("idx_ipc_msg_zone_path", "zone_id", "path", unique=True),
        # Directory listing lookups
        Index("idx_ipc_msg_zone_dir", "zone_id", "dir_path"),
    )

    def __repr__(self) -> str:
        kind = "dir" if self.is_dir else "file"
        return f"<IPCMessageModel({kind}, path={self.path}, zone={self.zone_id})>"
