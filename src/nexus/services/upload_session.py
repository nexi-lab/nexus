"""Upload session data model for chunked/resumable uploads (Issue #788).

Defines the UploadSession frozen dataclass and UploadStatus enum
for tus.io v1.0.0 compliant resumable upload protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class UploadStatus(StrEnum):
    """Status of a chunked upload session."""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    TERMINATED = "terminated"
    EXPIRED = "expired"


@dataclass(frozen=True)
class UploadSession:
    """Immutable record of a chunked upload session.

    Attributes:
        upload_id: Unique identifier for this upload session.
        target_path: Virtual path where the final file will be written.
        upload_length: Total declared file size in bytes.
        upload_offset: Current offset (bytes received so far).
        status: Current status of the upload.
        zone_id: Zone ID for permission scoping.
        user_id: User who initiated the upload.
        metadata: tus Upload-Metadata key-value pairs.
        checksum_algorithm: Preferred checksum algorithm (sha256, md5, crc32).
        created_at: When the session was created.
        expires_at: When the session expires (TTL-based).
        backend_upload_id: Backend-specific multipart upload ID (e.g. S3 UploadId).
        backend_name: Name of the backend handling this upload.
        parts_received: Number of chunk parts received so far.
        content_hash: Final content hash after assembly (BLAKE3).
    """

    upload_id: str
    target_path: str
    upload_length: int
    upload_offset: int = 0
    status: UploadStatus = UploadStatus.CREATED
    zone_id: str = "default"
    user_id: str = "anonymous"
    metadata: dict[str, str] = field(default_factory=dict)
    checksum_algorithm: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None
    backend_upload_id: str | None = None
    backend_name: str | None = None
    parts_received: int = 0
    content_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for persistence."""
        return {
            "upload_id": self.upload_id,
            "target_path": self.target_path,
            "upload_length": self.upload_length,
            "upload_offset": self.upload_offset,
            "status": self.status.value,
            "zone_id": self.zone_id,
            "user_id": self.user_id,
            "metadata": dict(self.metadata),
            "checksum_algorithm": self.checksum_algorithm,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "backend_upload_id": self.backend_upload_id,
            "backend_name": self.backend_name,
            "parts_received": self.parts_received,
            "content_hash": self.content_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UploadSession:
        """Deserialize from a plain dict."""
        created_at = data.get("created_at")
        if isinstance(created_at, str):
            created_at = datetime.fromisoformat(created_at)
        elif created_at is None:
            created_at = datetime.now(UTC)
        # SQLite strips timezone info — ensure UTC if naive
        if isinstance(created_at, datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

        expires_at = data.get("expires_at")
        if isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)
        # SQLite strips timezone info — ensure UTC if naive
        if isinstance(expires_at, datetime) and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)

        return cls(
            upload_id=data["upload_id"],
            target_path=data["target_path"],
            upload_length=data["upload_length"],
            upload_offset=data.get("upload_offset", 0),
            status=UploadStatus(data.get("status", "created")),
            zone_id=data.get("zone_id", "default"),
            user_id=data.get("user_id", "anonymous"),
            metadata=data.get("metadata", {}),
            checksum_algorithm=data.get("checksum_algorithm"),
            created_at=created_at,
            expires_at=expires_at,
            backend_upload_id=data.get("backend_upload_id"),
            backend_name=data.get("backend_name"),
            parts_received=data.get("parts_received", 0),
            content_hash=data.get("content_hash"),
        )

    @property
    def is_complete(self) -> bool:
        """Whether the upload has received all bytes."""
        return self.upload_offset >= self.upload_length

    @property
    def is_expired(self) -> bool:
        """Whether the session has passed its TTL."""
        if self.expires_at is None:
            return False
        return datetime.now(UTC) >= self.expires_at

    @property
    def remaining_bytes(self) -> int:
        """Bytes remaining to complete the upload."""
        return max(0, self.upload_length - self.upload_offset)
