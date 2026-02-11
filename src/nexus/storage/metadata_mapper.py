"""Central metadata mapping between FileMetadata (proto) and FilePathModel (SQLAlchemy).

Single source of truth for field name translations and type conversions.
Eliminates the DRY violation where 4+ locations hand-code field mappings.

Usage:
    from nexus.storage.metadata_mapper import MetadataMapper

    # Proto serialization
    proto_msg = MetadataMapper.to_proto(metadata)
    metadata = MetadataMapper.from_proto(proto_msg)

    # SQLAlchemy column values
    values = MetadataMapper.to_file_path_values(metadata)

    # JSON fallback serialization
    json_dict = MetadataMapper.to_json(metadata)
    metadata = MetadataMapper.from_json(json_dict)
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nexus.core._metadata_generated import FileMetadata

logger = logging.getLogger(__name__)


def _to_naive(dt: datetime | None) -> datetime | None:
    """Strip timezone from datetime (SQLite stores naive UTC)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def _utcnow_naive() -> datetime:
    """Return current UTC time as naive datetime (for SQLite compat)."""
    from datetime import UTC

    return datetime.now(UTC).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Field name mapping: proto field -> SQLAlchemy column
# ---------------------------------------------------------------------------

# Maps FileMetadata attribute names to FilePathModel column names.
# None = field exists in proto but not yet in FilePathModel.
PROTO_TO_SQL: dict[str, str | None] = {
    "path": "virtual_path",
    "backend_name": "backend_id",
    "physical_path": "physical_path",
    "size": "size_bytes",
    "etag": "content_hash",
    "mime_type": "file_type",
    "created_at": "created_at",
    "modified_at": "updated_at",
    "version": "current_version",
    "zone_id": "zone_id",
    "created_by": None,  # TODO(#1246): Add to FilePathModel
    "entry_type": None,  # TODO(#1246): Add to FilePathModel
    "target_zone_id": None,  # TODO(#1246): Add to FilePathModel
    "owner_id": "posix_uid",
}


class MetadataMapper:
    """Centralized mapping between FileMetadata and other representations.

    All field name translations and type conversions live here.
    Consumers should never hand-code field mappings.
    """

    # -- Proto serialization ------------------------------------------------

    @staticmethod
    def to_proto(metadata: FileMetadata) -> Any:
        """Convert FileMetadata dataclass to protobuf message.

        Returns:
            metadata_pb2.FileMetadata protobuf message.

        Raises:
            ImportError: If protobuf code not available.
        """
        from nexus.core import metadata_pb2

        return metadata_pb2.FileMetadata(
            path=metadata.path,
            backend_name=metadata.backend_name,
            physical_path=metadata.physical_path or "",
            size=metadata.size,
            etag=metadata.etag or "",
            mime_type=metadata.mime_type or "",
            created_at=metadata.created_at.isoformat() if metadata.created_at else "",
            modified_at=metadata.modified_at.isoformat() if metadata.modified_at else "",
            version=metadata.version,
            zone_id=metadata.zone_id or "",
            created_by=metadata.created_by or "",
            entry_type=metadata.entry_type,
            target_zone_id=metadata.target_zone_id or "",
            owner_id=metadata.owner_id or "",
        )

    @staticmethod
    def from_proto(proto: Any) -> FileMetadata:
        """Convert protobuf message to FileMetadata dataclass.

        Args:
            proto: metadata_pb2.FileMetadata protobuf message.

        Returns:
            FileMetadata dataclass.
        """
        from contextlib import suppress

        from nexus.core._metadata_generated import FileMetadata

        created_at = None
        modified_at = None
        if proto.created_at:
            with suppress(ValueError):
                created_at = datetime.fromisoformat(proto.created_at)
        if proto.modified_at:
            with suppress(ValueError):
                modified_at = datetime.fromisoformat(proto.modified_at)

        return FileMetadata(
            path=proto.path,
            backend_name=proto.backend_name,
            physical_path=proto.physical_path or proto.path,
            size=proto.size,
            etag=proto.etag or None,
            mime_type=proto.mime_type or None,
            created_at=created_at,
            modified_at=modified_at,
            version=proto.version,
            zone_id=proto.zone_id or None,
            created_by=proto.created_by or None,
            entry_type=proto.entry_type,
            target_zone_id=proto.target_zone_id or None,
            owner_id=proto.owner_id or None,
        )

    # -- JSON serialization -------------------------------------------------

    @staticmethod
    def to_json(metadata: FileMetadata) -> dict[str, Any]:
        """Convert FileMetadata to JSON-serializable dict.

        Uses proto field names (not SQL column names).
        """
        return {
            "path": metadata.path,
            "backend_name": metadata.backend_name,
            "physical_path": metadata.physical_path,
            "size": metadata.size,
            "etag": metadata.etag,
            "mime_type": metadata.mime_type,
            "created_at": metadata.created_at.isoformat() if metadata.created_at else None,
            "modified_at": metadata.modified_at.isoformat() if metadata.modified_at else None,
            "version": metadata.version,
            "zone_id": metadata.zone_id,
            "created_by": metadata.created_by,
            "entry_type": metadata.entry_type,
            "target_zone_id": metadata.target_zone_id,
            "owner_id": metadata.owner_id,
        }

    @staticmethod
    def from_json(obj: dict[str, Any]) -> FileMetadata:
        """Convert JSON dict to FileMetadata dataclass.

        Handles ISO 8601 timestamp parsing.
        """
        from nexus.core._metadata_generated import FileMetadata

        # Migration: convert legacy is_directory -> entry_type
        if "is_directory" in obj:
            is_dir = obj.pop("is_directory")
            if "entry_type" not in obj:
                obj["entry_type"] = 1 if is_dir else 0

        if obj.get("created_at"):
            obj["created_at"] = datetime.fromisoformat(obj["created_at"])
        if obj.get("modified_at"):
            obj["modified_at"] = datetime.fromisoformat(obj["modified_at"])
        return FileMetadata(**obj)

    # -- SQLAlchemy column values -------------------------------------------

    @staticmethod
    def to_file_path_values(
        metadata: FileMetadata,
        *,
        include_version: bool = True,
    ) -> dict[str, Any]:
        """Convert FileMetadata to dict of FilePathModel column values.

        Used for both INSERT and UPDATE operations.
        Keys are FilePathModel column names (not proto field names).

        Args:
            metadata: Source metadata.
            include_version: If True, include current_version=1.
                Set to False for updates where version is incremented separately.
        """
        values: dict[str, Any] = {
            "virtual_path": metadata.path,
            "backend_id": metadata.backend_name or "local",
            "physical_path": metadata.physical_path or metadata.path,
            "size_bytes": metadata.size or 0,
            "content_hash": metadata.etag,
            "file_type": metadata.mime_type,
            "created_at": _to_naive(metadata.created_at) or _utcnow_naive(),
            "updated_at": _to_naive(metadata.modified_at) or _utcnow_naive(),
            "zone_id": metadata.zone_id or "default",
            "posix_uid": metadata.owner_id,
        }
        if include_version:
            values["current_version"] = 1
        return values

    @staticmethod
    def to_file_path_update_values(metadata: FileMetadata) -> dict[str, Any]:
        """Convert FileMetadata to dict for UPDATE operations.

        Returns only the columns that should be updated (excludes version,
        which is handled separately via SQL increment).
        """
        return {
            "backend_id": metadata.backend_name,
            "physical_path": metadata.physical_path,
            "size_bytes": metadata.size or 0,
            "content_hash": metadata.etag,
            "file_type": metadata.mime_type,
            "updated_at": _to_naive(metadata.modified_at) or _utcnow_naive(),
        }
