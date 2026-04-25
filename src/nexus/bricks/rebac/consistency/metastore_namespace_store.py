"""Metastore-backed namespace configuration store for ReBAC.

Replaces ReBACNamespaceModel (SQLAlchemy ORM) and raw SQL against the
``rebac_namespaces`` table.  Stores per-object-type namespace configs
in the Metastore (redb) under a reserved path prefix.

Issue #183: Migrate ReBACNamespaceModel from RecordStore to Metastore.

Storage layout
--------------
Namespace records reuse the file-metadata KV slot keyed by
``ns:rebac:{object_type}``.  The ``ns:rebac:`` path prefix uniquely
identifies them — no per-record discriminator field is required.  The
namespace JSON envelope is stashed in ``etag`` (a Nullable string slot
the metastore already round-trips); etag's normal file-content-hash
semantics do not apply to these synthetic records, and nothing else in
the system reads ``etag`` for ``ns:rebac:``-prefixed paths.

Mirrors the pattern used by :mod:`nexus.bricks.mount.metastore_mount_store`.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

from nexus.contracts.metadata import FileMetadata

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import NamespaceConfig


class _MetastoreProto(Protocol):
    """Minimal protocol for metastore get/put/delete/list used by namespace store."""

    def get(self, path: str) -> FileMetadata | None: ...
    def put(self, metadata: FileMetadata) -> None: ...
    def delete(self, path: str) -> dict[str, Any] | None: ...
    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]: ...


logger = logging.getLogger(__name__)

_NS_PREFIX = "ns:rebac:"


def _payload_of(fm: FileMetadata) -> dict[str, Any] | None:
    """Decode the JSON envelope from ``etag``; return ``None`` if absent or malformed."""
    if not fm.etag:
        return None
    try:
        data = json.loads(fm.etag)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _record(key: str, payload: dict[str, Any]) -> FileMetadata:
    return FileMetadata(path=key, size=0, etag=json.dumps(payload))


class MetastoreNamespaceStore:
    """Namespace configuration store backed by MetastoreABC.

    Key pattern: ``ns:rebac:{object_type}`` → JSON envelope stashed in
    ``etag`` with fields: ``namespace_id``, ``object_type``, ``config``,
    ``created_at``, ``updated_at``.
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore

    def create_or_update(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration."""
        key = f"{_NS_PREFIX}{namespace.object_type}"
        payload: dict[str, Any] = {
            "namespace_id": namespace.namespace_id,
            "object_type": namespace.object_type,
        }
        # Best-effort capture of additional fields if present on the domain object.
        for attr in ("config", "created_at", "updated_at"):
            if hasattr(namespace, attr):
                value = getattr(namespace, attr)
                if value is not None and not callable(value):
                    payload[attr] = value
        self._metastore.put(_record(key, payload))

    def create_if_absent(self, namespace: NamespaceConfig) -> None:
        """Create namespace only if it does not already exist."""
        key = f"{_NS_PREFIX}{namespace.object_type}"
        existing = self._metastore.get(key)
        if existing is not None and _payload_of(existing) is not None:
            return
        self.create_or_update(namespace)

    def create_or_update_default(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace, but only if it matches our default namespace_id.

        This prevents overwriting custom namespaces created by tests or users.
        """
        key = f"{_NS_PREFIX}{namespace.object_type}"
        existing = self._metastore.get(key)
        if existing is not None:
            data = _payload_of(existing)
            if data is not None and data.get("namespace_id") != namespace.namespace_id:
                return  # Custom namespace, don't overwrite
        self.create_or_update(namespace)

    def get(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace configuration for an object type.

        Returns:
            Dict with keys: namespace_id, object_type, config, created_at, updated_at
            or None if not found.
        """
        fm = self._metastore.get(f"{_NS_PREFIX}{object_type}")
        if fm is None:
            return None
        return _payload_of(fm)

    def list_all(self) -> list[dict[str, Any]]:
        """List all namespace configurations.

        Returns:
            List of namespace dicts sorted by object_type.
        """
        entries = self._metastore.list(_NS_PREFIX)
        results: list[dict[str, Any]] = []
        for fm in entries:
            data = _payload_of(fm)
            if data is None:
                continue
            results.append(data)
        results.sort(key=lambda d: d.get("object_type", ""))
        return results

    def delete(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Returns:
            True if namespace was deleted, False if not found.
        """
        key = f"{_NS_PREFIX}{object_type}"
        existing = self._metastore.get(key)
        if existing is None or _payload_of(existing) is None:
            return False
        self._metastore.delete(key)
        return True
