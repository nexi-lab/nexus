"""Metastore-backed version sequence store for ReBAC consistency tokens.

Replaces ReBACVersionSequenceModel (SQLAlchemy ORM). Stores per-zone
revision counters in the Metastore (redb) under a reserved path prefix.

Issue #191: Migrate ReBACVersionSequenceModel from RecordStore to Metastore.

Storage layout
--------------
Each per-zone counter reuses the file-metadata KV slot keyed by
``/_internal/ver/rebac/{zone_id}``.  The path prefix uniquely identifies
these synthetic records — no per-record discriminator field is required.
The JSON envelope ``{"v": int}`` is stashed in ``etag`` (a Nullable string
slot the metastore already round-trips).  Mirrors the pattern used by
:mod:`nexus.bricks.mount.metastore_mount_store`.
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


class MetastoreVersionStore:
    """Per-zone version sequence backed by MetastoreABC.

    Key pattern: ``/_internal/ver/rebac/{zone_id}`` → JSON ``{"v": int}``
    stashed in ``etag``.
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore

    def get_version(self, zone_id: str) -> int:
        """Get current version for a zone. Returns 0 if not found."""
        fm = self._metastore.get(f"{_VER_PREFIX}{zone_id}")
        if fm is None or not fm.etag:
            return 0
        try:
            val: int = json.loads(fm.etag)["v"]
            return val
        except (json.JSONDecodeError, KeyError, TypeError):
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
            size=0,
            etag=json.dumps({"v": new_version}),
        )
        self._metastore.put(fm)
        return new_version
