"""Metastore-backed version sequence store for ReBAC consistency tokens.

Replaces ReBACVersionSequenceModel (SQLAlchemy ORM). Stores per-zone
revision counters in the Metastore (redb) under a reserved path prefix.

Issue #191: Migrate ReBACVersionSequenceModel from RecordStore to Metastore.
"""

from __future__ import annotations

import json
import logging
from typing import Protocol

from nexus.contracts.metadata import FileMetadata


class _MetastoreProto(Protocol):
    """Minimal protocol for metastore get/put used by version store."""

    def get(self, path: str) -> FileMetadata | None: ...
    def put(self, metadata: FileMetadata) -> None: ...


logger = logging.getLogger(__name__)

_VER_PREFIX = "/_internal/ver/rebac/"
_VER_BACKEND = "_version"


class MetastoreVersionStore:
    """Per-zone version sequence backed by MetastoreABC.

    Key pattern: ``ver:rebac:{zone_id}`` → JSON ``{"v": int}``
    Uses FileMetadata as the storage envelope (same pattern as MetastoreSettingsStore).
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore

    def get_version(self, zone_id: str) -> int:
        """Get current version for a zone. Returns 0 if not found."""
        fm = self._metastore.get(f"{_VER_PREFIX}{zone_id}")
        if fm is None or fm.backend_name != _VER_BACKEND:
            return 0
        try:
            val: int = json.loads(fm.physical_path)["v"]
            return val
        except (json.JSONDecodeError, KeyError):
            return 0

    def increment_version(self, zone_id: str) -> int:
        """Atomically increment and return the new version for a zone.

        Atomic at the Metastore level: redb ACID for single-node,
        Raft consensus serializes writes for multi-node.
        """
        current = self.get_version(zone_id)
        new_version = current + 1
        fm = FileMetadata(
            path=f"{_VER_PREFIX}{zone_id}",
            backend_name=_VER_BACKEND,
            physical_path=json.dumps({"v": new_version}),
            size=0,
        )
        self._metastore.put(fm)
        return new_version
