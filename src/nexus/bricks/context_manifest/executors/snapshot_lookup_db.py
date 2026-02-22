"""Snapshot lookup protocols and CAS manifest reader (Issue #1428).

Provides Protocol-based DI for snapshot retrieval and manifest reading.

SnapshotLookup: retrieve snapshot metadata by ID or latest.
ManifestReader: read file paths from a CAS-stored workspace manifest.

Concrete SQLAlchemy implementation (DatabaseSnapshotLookup) has been moved
to ``nexus.storage.repositories.snapshot_lookup`` (Issue #2189).
Re-exported here for backward compatibility.
"""

import json
import logging
from typing import Any, Protocol, runtime_checkable

from nexus.storage.repositories.snapshot_lookup import DatabaseSnapshotLookup

logger = logging.getLogger(__name__)


@runtime_checkable
class SnapshotLookup(Protocol):
    """Protocol for workspace snapshot retrieval."""

    def get_snapshot(self, snapshot_id: str) -> dict[str, Any] | None:
        """Get snapshot metadata by ID. Returns None if not found."""
        ...

    def get_latest_snapshot(self, workspace_path: str) -> dict[str, Any] | None:
        """Get the most recent snapshot for a workspace path. Returns None if none exist."""
        ...


@runtime_checkable
class ManifestReader(Protocol):
    """Protocol for reading workspace manifest file paths from CAS."""

    def read_file_paths(self, manifest_hash: str) -> list[str] | None:
        """Read file paths from a CAS-stored manifest. Returns None on failure."""
        ...


class CASManifestReader:
    """CAS-backed implementation of ManifestReader.

    Reads a workspace manifest from content-addressable storage and
    extracts the sorted list of file paths.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def read_file_paths(self, manifest_hash: str) -> list[str] | None:
        """Read file paths from a CAS-stored workspace manifest."""
        try:
            content = self._backend.read_content(manifest_hash)
            if content is None:
                logger.warning("Manifest hash %s not found in CAS", manifest_hash)
                return None

            raw = content if isinstance(content, bytes) else content.encode("utf-8")
            parsed = json.loads(raw)
            return sorted(parsed.keys())
        except Exception as exc:
            logger.warning("Failed to read manifest %s: %s", manifest_hash, exc)
            return None


__all__ = [
    "CASManifestReader",
    "DatabaseSnapshotLookup",
    "ManifestReader",
    "SnapshotLookup",
]
