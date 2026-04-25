"""Metastore-backed mount configuration store.

Replaces MountConfigModel (SQLAlchemy ORM) for persisting mount configs.
Stores mount configurations in the Metastore (redb) under a reserved path prefix.

Issue #192: Migrate MountConfigModel from RecordStore to Metastore.

Storage layout
--------------
Mount records reuse the file-metadata KV slot keyed by ``mnt:{mount_point}``.
The ``mnt:`` path prefix uniquely identifies them — record-level type tags
are unnecessary. The full mount config JSON is stashed in ``etag`` (a
Nullable string slot the metastore already round-trips). Etag's normal
file-content-hash semantics do not apply to these synthetic records, and
nothing else in the system reads ``etag`` for ``mnt:``-prefixed paths.

Migration note: a follow-up PR should move mount config off FileMetadata
into a dedicated KV abstraction (e.g. a small sqlite table or a raw redb
sub-store), at which point this kludge disappears.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)

_MNT_PREFIX = "mnt:"


class _MetastoreProto(Protocol):
    """Minimal protocol for metastore operations used by mount store."""

    def get(self, path: str) -> FileMetadata | None: ...
    def put(self, metadata: FileMetadata) -> None: ...
    def delete(self, path: str) -> dict[str, Any] | None: ...
    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]: ...


def _payload_of(fm: FileMetadata) -> dict[str, Any] | None:
    if not fm.etag:
        return None
    try:
        data = json.loads(fm.etag)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _record(key: str, payload: dict[str, Any]) -> FileMetadata:
    return FileMetadata(path=key, size=0, etag=json.dumps(payload))


class MetastoreMountStore:
    """Mount configuration store backed by MetastoreABC.

    Key pattern: ``mnt:{mount_point}`` → mount config JSON in ``etag``.
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
        if self._metastore.get(key) is not None:
            raise ValueError(f"Mount already exists at {mount_point}")

        now = datetime.now(UTC).isoformat()
        payload = {
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
        self._metastore.put(_record(key, payload))
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
        if existing is None:
            return False
        data = _payload_of(existing)
        if data is None:
            return False

        if backend_config is not None:
            data["backend_config"] = backend_config
        if description is not None:
            data["description"] = description
        if replication is not None:
            data["replication"] = replication
        data["updated_at"] = datetime.now(UTC).isoformat()

        self._metastore.put(_record(key, data))
        return True

    def get(self, mount_point: str) -> dict[str, Any] | None:
        """Get a mount configuration by mount_point."""
        fm = self._metastore.get(f"{_MNT_PREFIX}{mount_point}")
        if fm is None:
            return None
        return _payload_of(fm)

    def list_all(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all mount configurations with optional filters."""
        entries = self._metastore.list(_MNT_PREFIX)
        results: list[dict[str, Any]] = []
        for fm in entries:
            data = _payload_of(fm)
            if data is None:
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
        if self._metastore.get(key) is None:
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
