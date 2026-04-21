"""Metastore-backed mount configuration store.

Replaces MountConfigModel (SQLAlchemy ORM) for persisting mount configs.
Stores mount configurations in the Metastore (redb) under a reserved path prefix.

Issue #192: Migrate MountConfigModel from RecordStore to Metastore.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)

_MNT_PREFIX = "mnt:"
_MNT_BACKEND = "_mount_config"


class _MetastoreProto(Protocol):
    """Minimal protocol for metastore operations used by mount store."""

    def get(self, path: str) -> FileMetadata | None: ...
    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None: ...
    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None: ...
    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]: ...


class MetastoreMountStore:
    """Mount configuration store backed by MetastoreABC.

    Key pattern: ``mnt:{mount_point}`` → JSON envelope with all mount fields.
    Uses FileMetadata as the storage envelope.
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore

    def save(
        self,
        mount_id: str,
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
        owner_user_id: str | None = None,
        zone_id: str | None = None,
        description: str | None = None,
        replication: str | None = None,
    ) -> str:
        """Save a mount configuration. Raises ValueError if mount_point already exists."""
        self._validate(mount_point, backend_type, backend_config)

        key = f"{_MNT_PREFIX}{mount_point}"
        existing = self._metastore.get(key)
        if existing is not None and existing.backend_name.startswith(_MNT_BACKEND):
            raise ValueError(f"Mount already exists at {mount_point}")

        now = datetime.now(UTC).isoformat()
        payload = json.dumps(
            {
                "mount_id": mount_id,
                "mount_point": mount_point,
                "backend_type": backend_type,
                "backend_config": backend_config,
                "owner_user_id": owner_user_id,
                "zone_id": zone_id,
                "description": description,
                "replication": replication,
                "created_at": now,
                "updated_at": now,
            }
        )
        fm = FileMetadata(
            path=key,
            backend_name=_MNT_BACKEND,
            physical_path=payload,
            size=0,
        )
        self._metastore.put(fm)
        return mount_id

    def update(
        self,
        mount_point: str,
        backend_config: dict[str, Any] | None = None,
        description: str | None = None,
        replication: str | None = None,
    ) -> bool:
        """Update an existing mount configuration. Returns False if not found."""
        key = f"{_MNT_PREFIX}{mount_point}"
        existing = self._metastore.get(key)
        if existing is None or not existing.backend_name.startswith(_MNT_BACKEND):
            return False

        try:
            data: dict[str, Any] = json.loads(existing.physical_path)
        except (json.JSONDecodeError, KeyError):
            return False

        if backend_config is not None:
            data["backend_config"] = backend_config
        if description is not None:
            data["description"] = description
        if replication is not None:
            data["replication"] = replication
        data["updated_at"] = datetime.now(UTC).isoformat()

        fm = FileMetadata(
            path=key,
            backend_name=_MNT_BACKEND,
            physical_path=json.dumps(data),
            size=0,
        )
        self._metastore.put(fm)
        return True

    def get(self, mount_point: str) -> dict[str, Any] | None:
        """Get a mount configuration by mount_point."""
        fm = self._metastore.get(f"{_MNT_PREFIX}{mount_point}")
        if fm is None or not fm.backend_name.startswith(_MNT_BACKEND):
            return None
        try:
            data: dict[str, Any] = json.loads(fm.physical_path)
            return data
        except (json.JSONDecodeError, KeyError):
            return None

    def list_all(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all mount configurations with optional filters."""
        entries = self._metastore.list(_MNT_PREFIX)
        results: list[dict[str, Any]] = []
        for fm in entries:
            if not fm.backend_name.startswith(_MNT_BACKEND):
                continue
            try:
                data: dict[str, Any] = json.loads(fm.physical_path)
            except (json.JSONDecodeError, KeyError):
                continue
            if owner_user_id and data.get("owner_user_id") != owner_user_id:
                continue
            if zone_id and data.get("zone_id") != zone_id:
                continue
            results.append(data)
        results.sort(key=lambda d: d.get("mount_point", ""))
        return results

    def remove(self, mount_point: str) -> bool:
        """Remove a mount configuration. Returns False if not found."""
        key = f"{_MNT_PREFIX}{mount_point}"
        existing = self._metastore.get(key)
        if existing is None or not existing.backend_name.startswith(_MNT_BACKEND):
            return False
        self._metastore.delete(key)
        return True

    @staticmethod
    def _validate(
        mount_point: str,
        backend_type: str,
        backend_config: dict[str, Any],
    ) -> None:
        """Validate mount config fields."""
        if not mount_point:
            raise ValueError("mount_point is required")
        if not mount_point.startswith("/"):
            raise ValueError(f"mount_point must start with '/', got {mount_point!r}")
        if not backend_type:
            raise ValueError("backend_type is required")
        if not backend_config:
            raise ValueError("backend_config is required")
