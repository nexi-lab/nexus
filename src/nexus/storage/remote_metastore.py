"""RemoteMetastore — MetastoreABC proxy for REMOTE deployment profile.

Proxies metadata operations to a Nexus server over gRPC via ``RPCTransport``.
Shares the same transport instance as ``RemoteBackend``.

Server is the single source of truth (SSOT) for metadata — this class
is a stateless proxy, **not** a cache.  No local state, no invalidation.

Issue #844: Converge RemoteNexusFS → NexusFS(profile=REMOTE).
Issue #1133: Unified gRPC transport.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any

from nexus.contracts.metadata import FileMetadata
from nexus.core.metastore import MetastoreABC

if TYPE_CHECKING:
    from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


def _dict_to_file_metadata(d: dict[str, Any]) -> FileMetadata:
    """Convert a server response dict to a FileMetadata dataclass."""
    created_at = d.get("created_at")
    if isinstance(created_at, str):
        created_at = datetime.fromisoformat(created_at)
    modified_at = d.get("modified_at")
    if isinstance(modified_at, str):
        modified_at = datetime.fromisoformat(modified_at)

    return FileMetadata(
        path=d.get("path", ""),
        backend_name=d.get("backend_name", ""),
        physical_path=d.get("physical_path", ""),
        size=d.get("size", 0),
        etag=d.get("etag"),
        mime_type=d.get("mime_type"),
        created_at=created_at if isinstance(created_at, datetime) else None,
        modified_at=modified_at if isinstance(modified_at, datetime) else None,
        version=d.get("version", 1),
        zone_id=d.get("zone_id"),
        created_by=d.get("created_by"),
        owner_id=d.get("owner_id"),
        entry_type=d.get("entry_type", 0),
        target_zone_id=d.get("target_zone_id"),
        i_links_count=d.get("i_links_count", 0),
    )


class RemoteMetastore(MetastoreABC):
    """MetastoreABC implementation that proxies to a remote Nexus server.

    Uses ``RPCTransport`` (gRPC) for all RPC calls, with automatic retry
    handled by the transport layer.
    All metadata queries are forwarded to the server — no local state.

    Args:
        transport: Shared ``RPCTransport`` instance (gRPC channel).
    """

    def __init__(self, transport: "RPCTransport") -> None:
        self._transport = transport

    # === RPC Transport ===

    def _call_rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """Delegate RPC call to the shared transport."""
        return self._transport.call_rpc(method, params)

    # === MetastoreABC Implementation ===

    def get(self, path: str) -> FileMetadata | None:
        """Get metadata for a file by proxying ``stat`` to the server."""
        try:
            result = self._call_rpc("sys_stat", {"path": path})
        except Exception:
            return None
        if result is None:
            return None
        if isinstance(result, dict):
            # Server wraps response as {"metadata": {...}} — unwrap it.
            meta_dict = result.get("metadata", result)
            if isinstance(meta_dict, dict):
                return _dict_to_file_metadata(meta_dict)
        return None

    def put(self, metadata: FileMetadata, *, consistency: str = "sc") -> int | None:
        """Store metadata by proxying ``sys_setattr`` to the server.

        The *consistency* hint is forwarded so the server can honour it.
        Non-fatal: in REMOTE mode the server already owns metadata —
        failures here (e.g. during init) are logged but not raised.
        """
        try:
            self._call_rpc(
                "sys_setattr",
                {"path": metadata.path, "metadata": metadata.to_dict(), "consistency": consistency},
            )
        except Exception as exc:
            logger.debug("RemoteMetastore.put(%s) failed (non-fatal): %s", metadata.path, exc)
        return None

    def delete(self, path: str, *, consistency: str = "sc") -> dict[str, Any] | None:
        """Delete metadata by proxying ``delete`` to the server."""
        result = self._call_rpc("sys_unlink", {"path": path, "consistency": consistency})
        if isinstance(result, dict):
            return result
        return {"path": path}

    def exists(self, path: str) -> bool:
        """Check if metadata exists by proxying ``exists`` to the server."""
        result = self._call_rpc("sys_access", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("exists", False))
        return bool(result)

    def list(self, prefix: str = "", recursive: bool = True, **kwargs: Any) -> list[FileMetadata]:
        """List files by proxying ``list`` to the server."""
        params: dict[str, Any] = {"path": prefix, "recursive": recursive}
        if kwargs:
            params.update(kwargs)
        result = self._call_rpc("sys_readdir", params)
        if not result:
            return []

        items: list[Any] = []
        if isinstance(result, list):
            items = result
        elif isinstance(result, dict) and "items" in result:
            items = result["items"]

        metadata_list: list[FileMetadata] = []
        for item in items:
            if isinstance(item, dict) and "path" in item:
                metadata_list.append(_dict_to_file_metadata(item))
            elif isinstance(item, str):
                metadata_list.append(
                    FileMetadata(
                        path=item,
                        backend_name="remote",
                        physical_path=item,
                        size=0,
                    )
                )
        return metadata_list

    def rename_path(self, old_path: str, new_path: str) -> None:
        """Rename a file path in metadata via server RPC."""
        self._call_rpc("sys_rename", {"old_path": old_path, "new_path": new_path})

    def is_implicit_directory(self, path: str) -> bool:
        """Check if path is an implicit directory (has children but no explicit metadata)."""
        result = self._call_rpc("sys_is_directory", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("is_directory", False))
        return bool(result)

    def close(self) -> None:
        """No-op — transport lifecycle managed by factory."""
