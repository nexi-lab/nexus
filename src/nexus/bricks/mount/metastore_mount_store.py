"""VFS-backed mount configuration store.

Stores mount configurations as JSON files under ``/__sys__/mounts/`` —
the kernel-reserved system path namespace. Each mount config lives at
``/__sys__/mounts/{percent-encoded-mount-point}``.

Replaces the prior implementation that wrote directly to the kernel
metastore using a reserved key prefix (``mnt:``). Direct metastore
access from a brick is an ABC leak — the kernel does not expose
metastore as a public ABI. All persistence goes through public VFS
syscalls now (``sys_write``/``sys_read``/``sys_readdir``/``sys_unlink``)
which are implemented in the Rust kernel.

Naming note: the class name still says ``Metastore`` for source-history
continuity; the underlying storage is no longer the metastore itself
but the kernel's VFS — the file the kernel writes to is, internally,
still in the metastore, but reached through public APIs.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import quote, unquote

from nexus.contracts.constants import SYSTEM_PATH_PREFIX

logger = logging.getLogger(__name__)

_MOUNTS_DIR = f"{SYSTEM_PATH_PREFIX}mounts/"


def _config_path(mount_point: str) -> str:
    """Encode mount_point into a unique VFS path under ``/__sys__/mounts/``.

    Mount points contain ``/`` which would create directory hierarchy if
    used verbatim — percent-encode them so each config is a leaf file.
    """
    return _MOUNTS_DIR + quote(mount_point, safe="")


def _decode_filename(name: str) -> str:
    """Inverse of ``quote(mount_point, safe='')`` for ``list_all``."""
    return unquote(name)


class _NexusFSProto(Protocol):
    """Minimal NexusFS surface used by the mount store.

    We only need four syscalls — keeping the surface tight makes the
    test fixture easier (no full NexusFS needed in unit tests).
    """

    def sys_write(self, path: str, buf: bytes | str, **kwargs: Any) -> Any: ...
    def sys_read(self, path: str, **kwargs: Any) -> Any: ...
    def sys_readdir(self, path: str = "/", recursive: bool = True, **kwargs: Any) -> Any: ...
    def sys_unlink(self, path: str, **kwargs: Any) -> Any: ...


def _decode_read(result: Any) -> bytes | None:
    """Normalize ``sys_read`` output into raw bytes, or ``None`` if absent.

    ``sys_read`` returns a dict with ``content`` (bytes) and ``hit`` (bool)
    in the common path, but some adapters return raw bytes directly.
    """
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


class MetastoreMountStore:
    """Mount configuration store backed by VFS files.

    Each config is one JSON file at
    ``/__sys__/mounts/{percent-encoded-mount-point}``. The schema
    matches the prior dict layout so callers see no behavior change.
    """

    def __init__(self, nexus_fs: _NexusFSProto) -> None:
        self._nx = nexus_fs

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

        path = _config_path(mount_point)
        if self._exists(path):
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
        self._nx.sys_write(path, json.dumps(payload).encode("utf-8"))
        return mount_id

    def update(
        self,
        mount_point: str,
        backend_config: dict[str, Any] | None = None,
        description: str | None = None,
        replication: str | None = None,
    ) -> bool:
        """Update an existing mount configuration. Returns False if not found."""
        path = _config_path(mount_point)
        existing = self._read(path)
        if existing is None:
            return False

        if backend_config is not None:
            existing["backend_config"] = backend_config
        if description is not None:
            existing["description"] = description
        if replication is not None:
            existing["replication"] = replication
        existing["updated_at"] = datetime.now(UTC).isoformat()

        self._nx.sys_write(path, json.dumps(existing).encode("utf-8"))
        return True

    def get(self, mount_point: str) -> dict[str, Any] | None:
        """Get a mount configuration by mount_point."""
        return self._read(_config_path(mount_point))

    def list_all(
        self,
        owner_user_id: str | None = None,
        zone_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List all mount configurations with optional filters."""
        try:
            entries = self._nx.sys_readdir(_MOUNTS_DIR, recursive=False, details=False)
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.debug("list_all: sys_readdir(%s) failed: %s", _MOUNTS_DIR, exc)
            return []

        if entries is None:
            return []

        results: list[dict[str, Any]] = []
        for entry in entries:
            # sys_readdir returns either list[str] or list[dict]; handle both.
            name = entry["name"] if isinstance(entry, dict) else entry
            if not isinstance(name, str):
                continue
            # readdir may return full paths or just basenames depending on layer.
            basename = name.rsplit("/", 1)[-1]
            if not basename:
                continue
            full_path = _MOUNTS_DIR + basename
            data = self._read(full_path)
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
        path = _config_path(mount_point)
        if not self._exists(path):
            return False
        try:
            self._nx.sys_unlink(path)
        except FileNotFoundError:
            return False
        return True

    # -- helpers --

    def _read(self, path: str) -> dict[str, Any] | None:
        """Read a JSON config file or return None if absent / malformed."""
        try:
            result = self._nx.sys_read(path)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug("_read(%s) failed: %s", path, exc)
            return None
        raw = _decode_read(result)
        if raw is None:
            return None
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def _exists(self, path: str) -> bool:
        return self._read(path) is not None

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
