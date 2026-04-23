"""RemoteBackend — ObjectStoreABC proxy for federation content reads.

Proxies content operations to a remote Nexus peer over gRPC via ``RPCTransport``.
Implements the ``ObjectStoreABC`` interface so the kernel can run its natural
VFS pipeline for cross-zone content fetches.

Used by DriverLifecycleCoordinator.resolve_backend() for federation reads
when a local backend miss falls back to a remote origin node. NOT used for
REMOTE deployment profile — that is handled by Rust RemoteBackend installed
via ``sys_setattr(backend_type="remote")``.

Content deletion (``delete_content``) is a deliberate no-op: the kernel
always follows with ``metastore.delete(path)`` which triggers the
server-side delete pipeline.

Issue #844: Converge RemoteNexusFS → NexusFS(profile=REMOTE).
Issue #1133: Unified gRPC transport.
Issue #1134: REMOTE profile migrated to Rust kernel.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nexus.core.object_store import ObjectStoreABC, WriteResult
from nexus.remote.base_client import BaseRemoteNexusFS

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
    from nexus.remote.rpc_transport import RPCTransport

logger = logging.getLogger(__name__)


class RemoteBackend(ObjectStoreABC):
    """ObjectStoreABC implementation that proxies to a remote Nexus server.

    Uses ``RPCTransport`` (gRPC) for all RPC calls, with automatic retry
    (tenacity: 3 attempts, exponential backoff 1–10 s) handled by the
    transport layer.

    Args:
        transport: Shared ``RPCTransport`` instance (gRPC channel).
    """

    # RemoteBackend dispatch is handled via DT_EXTERNAL_STORAGE entry_type
    # in metastore, not via capabilities. The server knows the actual
    # backend type and handles reads correctly.
    capabilities: frozenset[str] = frozenset()

    def __init__(self, transport: "RPCTransport") -> None:
        super().__init__()
        self._transport = transport

        # Reuse BaseRemoteNexusFS error handling (static method access)
        self._error_handler = BaseRemoteNexusFS()

    # === Identity ===

    @property
    def name(self) -> str:
        return "remote"

    @property
    def has_root_path(self) -> bool:
        """Remote server always has a configured root path."""
        return True

    # === RPC Transport ===

    def _call_rpc(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> Any:
        """Delegate RPC call to the shared transport."""
        return self._transport.call_rpc(method, params, read_timeout=read_timeout)

    # === Path Resolution ===

    @staticmethod
    def _to_server_path(context: "OperationContext | None") -> str:
        """Extract server-absolute path from OperationContext.

        The kernel sets ``virtual_path`` (the full absolute nexus path) and
        ``backend_path`` (mount-stripped relative path) on the context before
        calling backend methods.  We prefer ``virtual_path`` because it is
        already absolute; fall back to ``backend_path`` with ``/`` prepended.
        """
        if context is not None:
            if context.virtual_path:
                return context.virtual_path
            if context.backend_path:
                bp = context.backend_path
                return bp if bp.startswith("/") else "/" + bp
        return "/"

    # === CAS Content Operations ===

    def write_content(
        self,
        content: bytes,
        content_id: str = "",
        *,
        offset: int = 0,
        context: OperationContext | None = None,
    ) -> WriteResult:
        path = self._to_server_path(context)
        result = self._transport.write_file(path, content)  # Typed RPC — raw bytes
        etag = result.get("etag", "")
        return WriteResult(
            content_id=etag,
            version=etag,
            size=result.get("size", len(content)),
        )

    def read_content(self, content_id: str, context: OperationContext | None = None) -> bytes:
        path = self._to_server_path(context)
        return self._transport.read_file(path, content_id=content_id)

    def delete_content(self, content_id: str, context: OperationContext | None = None) -> None:
        """No-op: server-side deletion is handled by RemoteMetastore.delete().

        The kernel always calls ``metastore.delete(path)`` after
        ``backend.delete_content()``.  In REMOTE mode, ``RemoteMetastore.delete``
        sends the ``delete`` RPC to the server which runs the full delete
        pipeline (CAS content + metadata).  Doing it here as well would be
        redundant and risks a double-delete race.
        """

    def get_content_size(self, content_id: str, context: OperationContext | None = None) -> int:
        path = self._to_server_path(context)
        result = self._call_rpc("sys_stat", {"path": path})
        size: int = int(result.get("size", 0)) if isinstance(result, dict) else 0
        return size

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        abs_path = path if path.startswith("/") else "/" + path
        self._call_rpc("mkdir", {"path": abs_path, "parents": parents, "exist_ok": exist_ok})

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        abs_path = path if path.startswith("/") else "/" + path
        self._call_rpc("sys_rmdir", {"path": abs_path, "recursive": recursive})

    # === Query Operations ===

    def content_exists(self, content_id: str, context: OperationContext | None = None) -> bool:
        path = self._to_server_path(context)
        result = self._call_rpc("access", {"path": path})
        if isinstance(result, dict):
            return bool(result.get("exists", False))
        return bool(result)

    def list_dir(self, path: str, context: OperationContext | None = None) -> list[str]:
        """List directory contents on the remote server."""
        abs_path = path if path.startswith("/") else "/" + path
        result = self._call_rpc("sys_readdir", {"path": abs_path})
        if isinstance(result, list):
            return [str(item) for item in result]
        if isinstance(result, dict):
            # The RPC handler returns a ``{"files": [...]}`` envelope
            # (``handle_list`` in ``server/rpc/handlers/filesystem.py``),
            # but earlier RemoteBackend code only checked for ``"items"``.
            # Accept both so list operations round-trip correctly.
            items: list[Any] | None = None
            if "files" in result:
                items = result["files"]
            elif "items" in result:
                items = result["items"]
            if items is not None:
                return [
                    str(item.get("path", item.get("name", "")))
                    if isinstance(item, dict)
                    else str(item)
                    for item in items
                ]
        return []

    # === Lifecycle ===

    def close(self) -> None:
        """No-op — transport lifecycle managed by factory."""
