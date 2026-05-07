"""VFS-backed namespace configuration store for ReBAC.

Stores per-object-type namespace configs as JSON files under
``/__sys__/rebac/namespaces/{object_type}`` — the kernel-reserved
system path namespace.

Replaces the prior implementation that wrote directly to the kernel
metastore using a reserved key prefix (``ns:rebac:``). Direct
metastore access from a brick is an ABC leak — bricks must use public
VFS syscalls. Issue #183 originally migrated this off SQLAlchemy onto
MetastoreABC; this revision moves it again onto VFS so the kernel
boundary is respected.

Layout
------
- One JSON file per namespace at
  ``/__sys__/rebac/namespaces/{object_type}``
- ``object_type`` is a single token without ``/`` (e.g. ``file``,
  ``group``, ``skill``) so no encoding is needed
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Protocol

from nexus.contracts.constants import SYSTEM_PATH_PREFIX
from nexus.contracts.types import OperationContext

if TYPE_CHECKING:
    from nexus.bricks.rebac.domain import NamespaceConfig


logger = logging.getLogger(__name__)

_NS_DIR = f"{SYSTEM_PATH_PREFIX}rebac/namespaces/"


def _system_ctx() -> OperationContext:
    """System context for kernel-internal mutations under /__sys__/.

    sys_unlink etc. now reject ``context=None`` for /__sys__/* paths
    (Issue #3786 hardening); rebac's namespace store is internal kernel
    code so it speaks with an explicit is_system token.
    """
    return OperationContext(user_id="system", groups=[], is_system=True)


def _config_path(object_type: str) -> str:
    """One JSON file per object_type under ``/__sys__/rebac/namespaces/``."""
    return _NS_DIR + object_type


def _json_default(o: Any) -> str:
    """Funnel ``datetime``/``date`` through ``isoformat`` for JSON encoding."""
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


class _NexusFSProto(Protocol):
    """Minimal NexusFS surface used by the namespace store."""

    def sys_write(self, path: str, buf: bytes | str, **kwargs: Any) -> Any: ...
    def sys_read(self, path: str, **kwargs: Any) -> Any: ...
    def sys_readdir(self, path: str = "/", recursive: bool = True, **kwargs: Any) -> Any: ...
    def sys_unlink(self, path: str, **kwargs: Any) -> Any: ...


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


class MetastoreNamespaceStore:
    """Namespace configuration store backed by VFS files.

    Public API unchanged from prior revisions — callers (``ReBACManager``,
    ``SearchService``, factory wiring) see no behavior change.
    """

    def __init__(self, nexus_fs: _NexusFSProto) -> None:
        self._nx = nexus_fs

    def create_or_update(self, namespace: NamespaceConfig) -> None:
        """Create or update a namespace configuration."""
        payload: dict[str, Any] = {
            "namespace_id": namespace.namespace_id,
            "object_type": namespace.object_type,
        }
        for attr in ("config", "created_at", "updated_at"):
            if hasattr(namespace, attr):
                value = getattr(namespace, attr)
                if value is not None and not callable(value):
                    payload[attr] = value
        path = _config_path(namespace.object_type)
        self._nx.sys_write(path, json.dumps(payload, default=_json_default).encode("utf-8"))

    def create_if_absent(self, namespace: NamespaceConfig) -> None:
        """Create namespace only if it does not already exist."""
        if self._read(_config_path(namespace.object_type)) is not None:
            return
        self.create_or_update(namespace)

    def create_or_update_default(self, namespace: NamespaceConfig) -> None:
        """Create or update a default namespace, but only if no
        custom namespace with a different ``namespace_id`` is already
        registered. Prevents overwriting user-defined namespaces."""
        existing = self._read(_config_path(namespace.object_type))
        if existing is not None and existing.get("namespace_id") != namespace.namespace_id:
            return
        self.create_or_update(namespace)

    def get(self, object_type: str) -> dict[str, Any] | None:
        """Get namespace configuration for an object type."""
        return self._read(_config_path(object_type))

    def list_all(self) -> list[dict[str, Any]]:
        """List all namespace configurations sorted by object_type."""
        try:
            entries = self._nx.sys_readdir(_NS_DIR, recursive=False, details=False)
        except FileNotFoundError:
            return []
        except Exception as exc:  # noqa: BLE001
            logger.debug("list_all: sys_readdir(%s) failed: %s", _NS_DIR, exc)
            return []

        if entries is None:
            return []

        results: list[dict[str, Any]] = []
        for entry in entries:
            name = entry["name"] if isinstance(entry, dict) else entry
            if not isinstance(name, str):
                continue
            basename = name.rsplit("/", 1)[-1]
            if not basename:
                continue
            data = self._read(_NS_DIR + basename)
            if data is None:
                continue
            results.append(data)

        results.sort(key=lambda d: d.get("object_type", ""))
        return results

    def delete(self, object_type: str) -> bool:
        """Delete a namespace configuration. Returns False if not found."""
        path = _config_path(object_type)
        if self._read(path) is None:
            return False
        try:
            self._nx.sys_unlink(path, context=_system_ctx())
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
