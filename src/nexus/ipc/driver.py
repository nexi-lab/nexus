"""VFS Backend driver for the /agents/ mount point.

IPCVFSDriver bridges the CAS-oriented ``Backend`` ABC with path-oriented
IPC storage. When mounted at ``/agents`` via the PathRouter, it intercepts
all file operations on agent IPC paths and delegates to an
``IPCStorageDriver`` instance.

This is a **virtual filesystem backend** (``has_virtual_filesystem = True``):
- ``read_content`` / ``write_content`` operate on paths, not content hashes
- ``list_dir`` returns agent names, subdirectories, or message files
- ``mkdir`` / ``rmdir`` / ``is_directory`` manage the IPC directory tree

The driver itself is intentionally thin — delivery logic (backpressure,
dedup, TTL, EventBus) lives in ``MessageSender`` and ``MessageProcessor``.

Issue: #1243
Architecture: KERNEL-ARCHITECTURE.md
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import Backend, HandlerStatusResponse
from nexus.core.response import HandlerResponse

if TYPE_CHECKING:
    from nexus.core.permissions import OperationContext
    from nexus.core.permissions_enhanced import EnhancedOperationContext
    from nexus.ipc.storage.protocol import IPCStorageDriver

logger = logging.getLogger(__name__)


class IPCVFSDriver(Backend):
    """VFS backend for the ``/agents/`` mount point.

    Intercepts file operations on agent IPC paths and delegates storage
    to an ``IPCStorageDriver`` instance. When mounted in the PathRouter,
    agents gain VFS-level access to their inboxes, outboxes, and cards.

    Args:
        storage: Pluggable storage backend (VFS, PostgreSQL, in-memory).
        zone_id: Zone ID for multi-tenant isolation.
        event_publisher: Optional EventBus publisher for notifications.
        max_inbox_size: Maximum messages per inbox for backpressure.
    """

    def __init__(
        self,
        storage: IPCStorageDriver,
        zone_id: str = "default",
        event_publisher: Any | None = None,
        max_inbox_size: int = 1000,
    ) -> None:
        self._storage = storage
        self._zone_id = zone_id
        self._publisher = event_publisher
        self._max_inbox_size = max_inbox_size
        # Long-lived event loop for sync-to-async bridging (HIGH-1 fix).
        # The Backend ABC is sync, but IPCStorageDriver is async.
        # A single background loop avoids per-call ThreadPoolExecutor overhead
        # and keeps event-loop-bound resources (e.g., asyncpg pools) working.
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._bg_thread: threading.Thread | None = None

    # === Identity & Capability Flags ===

    @property
    def name(self) -> str:
        return "ipc"

    @property
    def has_virtual_filesystem(self) -> bool:
        return True

    @property
    def supports_rename(self) -> bool:
        return True

    # === Connection (no-op for IPC) ===

    def connect(self, context: OperationContext | None = None) -> HandlerStatusResponse:
        return HandlerStatusResponse(success=True, details={"backend": "ipc"})

    # === Content Operations (path-oriented virtual FS) ===

    def write_content(
        self,
        content: bytes,
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]:
        """Write content and return a hash.

        For IPC, we store content at a generated path and return a
        content hash. Callers that need path-specific writes should
        use ``write_path()`` instead.
        """
        content_hash = hashlib.sha256(content).hexdigest()
        try:
            self._run_async(self._storage.write(f"/_cas/{content_hash}", content, self._zone_id))
            return HandlerResponse.ok(data=content_hash, backend_name="ipc")
        except Exception as exc:
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc")

    def write_path(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None = None,
    ) -> HandlerResponse[str]:
        """Write content to a specific VFS path.

        This is the primary write method for the IPC driver — messages
        and agent cards are always written to known paths.
        """
        try:
            self._run_async(self._storage.write(path, content, self._zone_id))
            content_hash = hashlib.sha256(content).hexdigest()
            return HandlerResponse.ok(data=content_hash, backend_name="ipc", path=path)
        except Exception as exc:
            logger.error("IPC write failed at %s: %s", path, exc)
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc", path=path)

    def read_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bytes]:
        """Read content by path (virtual filesystem mode).

        In VFS mode, ``content_hash`` is actually the virtual path.
        """
        path = content_hash  # In virtual FS mode, hash IS the path
        try:
            data = self._run_async(self._storage.read(path, self._zone_id))
            return HandlerResponse.ok(data=data, backend_name="ipc", path=path)
        except FileNotFoundError:
            return HandlerResponse.error(
                message=f"Not found: {path}",
                code=404,
                is_expected=True,
                backend_name="ipc",
                path=path,
            )
        except Exception as exc:
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc", path=path)

    def delete_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Delete content — IPC never hard-deletes (moves to dead_letter)."""
        return HandlerResponse.ok(data=None, backend_name="ipc")

    def content_exists(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if a path exists (virtual FS mode)."""
        path = content_hash
        try:
            exists = self._run_async(self._storage.exists(path, self._zone_id))
            return HandlerResponse.ok(data=exists, backend_name="ipc", path=path)
        except Exception as exc:
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc", path=path)

    def get_content_size(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Get file size by reading the content (no separate size API)."""
        path = content_hash
        try:
            data = self._run_async(self._storage.read(path, self._zone_id))
            return HandlerResponse.ok(data=len(data), backend_name="ipc", path=path)
        except FileNotFoundError:
            return HandlerResponse.error(
                message=f"Not found: {path}",
                code=404,
                is_expected=True,
                backend_name="ipc",
                path=path,
            )
        except Exception as exc:
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc", path=path)

    def get_ref_count(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[int]:
        """Reference count — always 1 for path-based storage."""
        return HandlerResponse.ok(data=1, backend_name="ipc")

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Create a directory in IPC storage."""
        try:
            self._run_async(self._storage.mkdir(path, self._zone_id))
            return HandlerResponse.ok(data=None, backend_name="ipc", path=path)
        except Exception as exc:
            return HandlerResponse.error(message=str(exc), code=500, backend_name="ipc", path=path)

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | EnhancedOperationContext | None = None,
    ) -> HandlerResponse[None]:
        """Remove directory — not supported for IPC (audit preservation)."""
        return HandlerResponse.error(
            message="IPC directories cannot be removed (audit trail preservation)",
            code=403,
            is_expected=True,
            backend_name="ipc",
            path=path,
        )

    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> HandlerResponse[bool]:
        """Check if a path is a directory in IPC storage."""
        try:
            # If list_dir succeeds, the path is a directory
            self._run_async(self._storage.list_dir(path, self._zone_id))
            return HandlerResponse.ok(data=True, backend_name="ipc", path=path)
        except FileNotFoundError:
            # Not a directory — could be a file or nonexistent
            return HandlerResponse.ok(data=False, backend_name="ipc", path=path)
        except Exception:
            return HandlerResponse.ok(data=False, backend_name="ipc", path=path)

    def list_dir(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> list[str]:
        """List directory contents."""
        result: list[str] = self._run_async(self._storage.list_dir(path, self._zone_id))
        return result

    # === ReBAC Object Type Mapping ===

    def get_object_type(self, backend_path: str) -> str:
        """Map IPC backend paths to ReBAC object types."""
        if backend_path.endswith(".json") and "AGENT.json" in backend_path:
            return "ipc:agent"
        if backend_path.endswith(".json"):
            return "ipc:message"
        return "ipc:directory"

    def get_object_id(self, backend_path: str) -> str:
        """Map backend path to ReBAC object identifier."""
        return backend_path

    # === Internal Helpers ===

    def _ensure_bg_loop(self) -> asyncio.AbstractEventLoop:
        """Ensure the background event loop and thread are running."""
        if self._bg_loop is None or not self._bg_loop.is_running():
            self._bg_loop = asyncio.new_event_loop()
            self._bg_thread = threading.Thread(
                target=self._bg_loop.run_forever,
                daemon=True,
                name="ipc-driver-loop",
            )
            self._bg_thread.start()
        return self._bg_loop

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine from sync Backend methods.

        The Backend ABC uses sync methods, but IPCStorageDriver is async.
        Uses a long-lived background event loop to avoid per-call overhead
        and keep event-loop-bound resources (asyncpg pools) working.
        """
        try:
            asyncio.get_running_loop()
            # We're inside a running event loop — dispatch to background loop
            loop = self._ensure_bg_loop()
            future = asyncio.run_coroutine_threadsafe(coro, loop)
            return future.result(timeout=30)
        except RuntimeError:
            # No running loop — safe to use asyncio.run directly
            return asyncio.run(coro)
