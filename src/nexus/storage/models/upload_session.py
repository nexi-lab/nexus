"""UploadSessionModel — SQLAlchemy model for chunked upload sessions (Issue #788).

Persists tus.io upload session state for resumable uploads.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default


class UploadSessionModel(Base):
    """Persistent storage for chunked upload sessions.

    Stores session state to survive server restarts and enable
    resumable uploads across connections.
    """

    __tablename__ = "upload_sessions"

    # Primary key
    upload_id: Mapped[str] = mapped_column(
        String(36),
        primary_key=True,
        default=_generate_uuid,
        server_default=_get_uuid_server_default(),
    )

    # Upload target
    target_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    upload_length: Mapped[int] = mapped_column(Integer, nullable=False)
    upload_offset: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Status
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="created")

    # Identity
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, default="anonymous")

    # tus metadata (JSON-encoded)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Checksum
    checksum_algorithm: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Backend integration
    backend_upload_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    backend_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Progress
    parts_received: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    content_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime.now(UTC)
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Indexes
    __table_args__ = (
        Index("idx_upload_sessions_status", "status"),
        Index("idx_upload_sessions_expires_at", "expires_at"),
        Index("idx_upload_sessions_zone_user", "zone_id", "user_id"),
    )

    def to_session_dict(self) -> dict[str, Any]:
        """Convert to dict compatible with UploadSession.from_dict()."""
        metadata: dict[str, str] = {}
        if self.metadata_json:
            metadata = json.loads(self.metadata_json)

        return {
            "upload_id": self.upload_id,
            "target_path": self.target_path,
            "upload_length": self.upload_length,
            "upload_offset": self.upload_offset,
            "status": self.status,
            "zone_id": self.zone_id,
            "user_id": self.user_id,
            "metadata": metadata,
            "checksum_algorithm": self.checksum_algorithm,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "backend_upload_id": self.backend_upload_id,
            "backend_name": self.backend_name,
            "parts_received": self.parts_received,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_upload_session(cls, session: Any) -> UploadSessionModel:
        """Create model from an UploadSession dataclass."""
        metadata_json = json.dumps(session.metadata) if session.metadata else None
        return cls(
            upload_id=session.upload_id,
            target_path=session.target_path,
            upload_length=session.upload_length,
            upload_offset=session.upload_offset,
            status=session.status.value if hasattr(session.status, "value") else session.status,
            zone_id=session.zone_id,
            user_id=session.user_id,
            metadata_json=metadata_json,
            checksum_algorithm=session.checksum_algorithm,
            created_at=session.created_at,
            expires_at=session.expires_at,
            backend_upload_id=session.backend_upload_id,
            backend_name=session.backend_name,
            parts_received=session.parts_received,
            content_hash=session.content_hash,
        )

    def __repr__(self) -> str:
        return (
            f"<UploadSessionModel(upload_id={self.upload_id}, "
            f"target_path={self.target_path}, status={self.status})>"
        )
