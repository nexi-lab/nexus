"""VFS-backed per-zone version sequence store for ReBAC consistency tokens.

Stores per-zone revision counters as JSON files under
``/__sys__/rebac/versions/{zone_id}`` — the kernel-reserved system path
namespace.

Replaces the prior implementation that wrote directly to the kernel
metastore using a reserved key prefix (``/_internal/ver/rebac/``).
Direct metastore access from a brick is an ABC leak — bricks must use
public VFS syscalls. Issue #191 originally migrated this off SQLAlchemy
onto MetastoreABC; this revision moves it again onto VFS so the kernel
boundary is respected.

Atomicity: ``increment_version`` is a non-atomic read-modify-write at
the Python layer. Single-node operation is sequential within a zone
(callers serialize); multi-node operation relies on the kernel's
write-path serialization (Raft for federated zones, redb ACID for
single-node) to make concurrent writes well-defined. The semantic is
"monotonically increasing per zone" — that's preserved.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from nexus.contracts.constants import SYSTEM_PATH_PREFIX

logger = logging.getLogger(__name__)

_VER_DIR = f"{SYSTEM_PATH_PREFIX}rebac/versions/"


def _version_path(zone_id: str) -> str:
    """One JSON file per zone under ``/__sys__/rebac/versions/``."""
    return _VER_DIR + zone_id


class _NexusFSProto(Protocol):
    """Minimal NexusFS surface used by the version store."""

    def sys_write(self, path: str, buf: bytes | str, **kwargs: Any) -> Any: ...
    def sys_read(self, path: str, **kwargs: Any) -> Any: ...


def _decode_read(result: Any) -> bytes | None:
    """Normalize ``sys_read`` output into raw bytes, or ``None`` if absent."""
    if result is None:
        return None
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    if isinstance(result, dict):
        if not result.get("hit", True):
            return None
        content = result.get("content")
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
    return None


class MetastoreVersionStore:
    """Per-zone version sequence backed by VFS files.

    Each zone has one JSON file at ``/__sys__/rebac/versions/{zone_id}``
    with payload ``{"v": int}``.
    """

    def __init__(self, nexus_fs: _NexusFSProto) -> None:
        self._nx = nexus_fs

    def get_version(self, zone_id: str) -> int:
        """Get current version for a zone. Returns 0 if not found."""
        path = _version_path(zone_id)
        try:
            result = self._nx.sys_read(path)
        except FileNotFoundError:
            return 0
        except Exception as exc:  # noqa: BLE001
            logger.debug("get_version(%s): sys_read failed: %s", zone_id, exc)
            return 0
        raw = _decode_read(result)
        if raw is None:
            return 0
        try:
            payload = json.loads(raw.decode("utf-8"))
            return int(payload["v"])
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return 0

    def increment_version(self, zone_id: str) -> int:
        """Increment and return the new version for a zone.

        Read-modify-write at this layer; the kernel write path serializes
        concurrent writers (Raft / redb).
        """
        current = self.get_version(zone_id)
        new_version = current + 1
        path = _version_path(zone_id)
        self._nx.sys_write(path, json.dumps({"v": new_version}).encode("utf-8"))
        return new_version
