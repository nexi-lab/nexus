"""Replication policy resolver — maps VFS paths to replication policies.

Reads mount configurations from MetastoreMountStore and resolves
which replication policy (if any) applies to a given path via
longest-prefix matching.

Used by ContentReplicationService to determine which paths need
content replication across Voters.

Supported policies:
    - ``"all-voters"``: replicate content to all Voter nodes in the zone.
    - ``None``: no replication (default).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from nexus.contracts.metadata import FileMetadata

logger = logging.getLogger(__name__)

_MNT_PREFIX = "mnt:"
_MNT_BACKEND = "_mount_config"


class _MetastoreProto(Protocol):
    def list(
        self, prefix: str = "", recursive: bool = True, **kwargs: Any
    ) -> list[FileMetadata]: ...


class ReplicationPolicyResolver:
    """Resolves replication policy for a given path from mount configs.

    Caches mount_point → policy mappings. Call ``refresh()`` to reload
    from metastore (typically done each scan cycle by ContentReplicationService).
    """

    def __init__(self, metastore: _MetastoreProto) -> None:
        self._metastore = metastore
        self._cache: dict[str, str | None] = {}  # mount_point → policy

    def refresh(self) -> None:
        """Reload mount configs from metastore and rebuild policy cache."""
        new_cache: dict[str, str | None] = {}
        entries = self._metastore.list(_MNT_PREFIX)
        for fm in entries:
            if fm.backend_name != _MNT_BACKEND:
                continue
            try:
                data: dict[str, Any] = json.loads(fm.physical_path)
            except (json.JSONDecodeError, KeyError):
                continue
            mount_point = data.get("mount_point", "")
            policy = data.get("replication")
            if mount_point:
                new_cache[mount_point] = policy
        self._cache = new_cache

    def get_policy(self, path: str) -> str | None:
        """Return replication policy for path, or None if not replicated.

        Uses longest-prefix matching against mount points.
        """
        best_match: str | None = None
        best_len = 0
        for mount_point in self._cache:
            if path.startswith(mount_point) and len(mount_point) > best_len:
                best_match = mount_point
                best_len = len(mount_point)
        if best_match is None:
            return None
        return self._cache[best_match]

    def get_replicated_prefixes(self) -> list[str]:
        """Return all mount points that have a replication policy set."""
        return [mp for mp, policy in self._cache.items() if policy is not None]
