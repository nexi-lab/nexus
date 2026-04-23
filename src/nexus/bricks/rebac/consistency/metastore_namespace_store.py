"""Metastore-backed namespace configuration store for ReBAC.

Replaces ReBACNamespaceModel (SQLAlchemy ORM) and raw SQL against the
``rebac_namespaces`` table.  Stores per-object-type namespace configs
in the Metastore (redb) under a reserved path prefix.

Issue #183: Migrate ReBACNamespaceModel from RecordStore to Metastore.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
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
_NS_BACKEND = "_namespace"


class MetastoreNamespaceStore:
    """Namespace configuration store backed by MetastoreABC.

    Key pattern: ``ns:rebac:{object_type}`` → JSON envelope with fields:
        namespace_id, object_type, config, created_at, updated_at

    Uses FileMetadata as the storage envelope (same pattern as MetastoreVersionStore).
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore

    def create_or_update(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration."""
        payload = json.dumps(
            {
                "namespace_id": namespace.namespace_id,
                "object_type": namespace.object_type,
                "config": namespace.config,
                "created_at": namespace.created_at.isoformat(),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        fm = FileMetadata(
            path=f"{_NS_PREFIX}{namespace.object_type}",
            backend_name=_NS_BACKEND,
            physical_path=payload,
            size=0,
        )
        self._metastore.put(fm)

    def create_if_absent(self, namespace: NamespaceConfig) -> None:
        """Create namespace only if it does not already exist."""
        key = f"{_NS_PREFIX}{namespace.object_type}"
        existing = self._metastore.get(key)
        if existing is not None and existing.backend_name.startswith(_NS_BACKEND):
            return
        self.create_or_update(namespace)

    def create_or_update_default(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace, but only if it matches our default namespace_id.

        This prevents overwriting custom namespaces created by tests or users.
        """
        key = f"{_NS_PREFIX}{namespace.object_type}"
        existing = self._metastore.get(key)
        if existing is not None and existing.backend_name.startswith(_NS_BACKEND):
            try:
                data = json.loads(existing.physical_path)
                if data.get("namespace_id") != namespace.namespace_id:
                    return  # Custom namespace, don't overwrite
            except (json.JSONDecodeError, KeyError):
                pass
        self.create_or_update(namespace)

    def get(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace configuration for an object type.

        Returns:
            Dict with keys: namespace_id, object_type, config, created_at, updated_at
            or None if not found.
        """
        fm = self._metastore.get(f"{_NS_PREFIX}{object_type}")
        if fm is None or not fm.backend_name.startswith(_NS_BACKEND):
            return None
        try:
            data: dict[str, Any] = json.loads(fm.physical_path)
            return data
        except (json.JSONDecodeError, KeyError):
            return None

    def list_all(self) -> list[dict[str, Any]]:
        """List all namespace configurations.

        Returns:
            List of namespace dicts sorted by object_type.
        """
        entries = self._metastore.list(_NS_PREFIX)
        results: list[dict[str, Any]] = []
        for fm in entries:
            if not fm.backend_name.startswith(_NS_BACKEND):
                continue
            try:
                data: dict[str, Any] = json.loads(fm.physical_path)
                results.append(data)
            except (json.JSONDecodeError, KeyError):
                continue
        results.sort(key=lambda d: d.get("object_type", ""))
        return results

    def delete(self, object_type: str) -> bool:
        """Delete a namespace configuration.

        Returns:
            True if namespace was deleted, False if not found.
        """
        key = f"{_NS_PREFIX}{object_type}"
        existing = self._metastore.get(key)
        if existing is None or not existing.backend_name.startswith(_NS_BACKEND):
            return False
        self._metastore.delete(key)
        return True
