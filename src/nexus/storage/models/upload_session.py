"""UploadSessionModel — SQLAlchemy model for chunked upload sessions (Issue #788).

Persists tus.io upload session state for resumable uploads.
"""

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from nexus.contracts.constants import ROOT_ZONE_ID
from nexus.storage.models._base import Base, _generate_uuid, _get_uuid_server_default

_UPLOAD_METADATA_STATE_VERSION = 1


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
    zone_id: Mapped[str] = mapped_column(String(255), nullable=False, default=ROOT_ZONE_ID)
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
    content_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

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
        metadata, parts = self._decode_metadata_state(self.metadata_json)

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
            "parts": parts,
            "content_id": self.content_id,
        }

    @classmethod
    def from_upload_session(cls, session: Any) -> "UploadSessionModel":
        """Create model from an UploadSession dataclass."""
        metadata_json = cls.encode_metadata_state(
            session.metadata,
            getattr(session, "parts", ()),
        )
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
            content_id=session.content_id,
        )

    @classmethod
    def encode_metadata_state(
        cls,
        metadata: dict[str, str] | None,
        parts: tuple[dict[str, Any], ...] | list[dict[str, Any]] | None,
    ) -> str:
        """Encode tus metadata plus server-owned part state into one legacy column."""
        return json.dumps(
            {
                "_nexus_upload_state_version": _UPLOAD_METADATA_STATE_VERSION,
                "metadata": metadata or {},
                "parts": [dict(part) for part in (parts or ())],
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _decode_metadata_state(raw: str | None) -> tuple[dict[str, str], list[dict[str, Any]]]:
        if not raw:
            return {}, []

        payload = json.loads(raw)
        if (
            isinstance(payload, dict)
            and payload.get("_nexus_upload_state_version") == _UPLOAD_METADATA_STATE_VERSION
        ):
            metadata = payload.get("metadata") or {}
            parts = payload.get("parts") or []
        else:
            metadata = payload if isinstance(payload, dict) else {}
            parts = []

        return (
            {str(key): str(value) for key, value in metadata.items()},
            [dict(part) for part in parts if isinstance(part, dict)],
        )

    def __repr__(self) -> str:
        return (
            f"<UploadSessionModel(upload_id={self.upload_id}, "
            f"target_path={self.target_path}, status={self.status})>"
        )
