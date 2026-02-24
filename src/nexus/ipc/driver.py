"""VFS Backend driver for the /agents/ mount point.

IPCVFSDriver bridges the CAS-oriented ``Backend`` ABC with path-oriented
IPC storage. When mounted at ``/agents`` via the PathRouter, it intercepts
all file operations on agent IPC paths and delegates to an
``IPCStorageDriver`` instance.

This is an **external-content backend** (``EXTERNAL_CONTENT`` capability):
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
from nexus.contracts.exceptions import BackendError, NexusFileNotFoundError
from nexus.core.object_store import WriteResult

if TYPE_CHECKING:
    from nexus.contracts.types import OperationContext
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
        *,
        zone_id: str,
        event_publisher: Any | None = None,
        max_inbox_size: int = 1000,
        timeout: float = 30,
    ) -> None:
        self._storage = storage
        self._zone_id = zone_id
        self._publisher = event_publisher
        self._max_inbox_size = max_inbox_size
        self._timeout = timeout
        # Eagerly create sync-to-async bridge — immutable after init.
        # The Backend ABC is sync, but IPCStorageDriver is async.
        # A single background loop avoids per-call ThreadPoolExecutor overhead
        # and keeps event-loop-bound resources (e.g., asyncpg pools) working.
        self._bg_loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._bg_thread = threading.Thread(
            target=self._bg_loop.run_forever,
            daemon=True,
            name="ipc-driver-loop",
        )
        self._bg_thread.start()

    def close(self) -> None:
        """Stop the background event loop and join the thread.

        Safe to call multiple times. Must be called before discarding
        the driver to prevent thread/loop leaks.
        """
        if self._bg_loop.is_running():
            self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)
            self._bg_thread.join(timeout=5)
            self._bg_loop.close()

    def __del__(self) -> None:
        """Safety net — stop the loop if close() was never called."""
        if self._bg_loop.is_running():
            self._bg_loop.call_soon_threadsafe(self._bg_loop.stop)

    # === Identity & Capability Flags ===

    @property
    def name(self) -> str:
        return "ipc"

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
    ) -> WriteResult:
        """Write content and return a WriteResult.

        For IPC, we store content at a generated path and return a
        content hash. Callers that need path-specific writes should
        use ``write_path()`` instead.
        """
        content_hash = hashlib.sha256(content).hexdigest()
        self._run_async(self._storage.write(f"/_cas/{content_hash}", content, self._zone_id))
        return WriteResult(content_hash=content_hash, size=len(content))

    def write_path(
        self,
        path: str,
        content: bytes,
        context: OperationContext | None = None,
    ) -> str:
        """Write content to a specific VFS path.

        This is the primary write method for the IPC driver — messages
        and agent cards are always written to known paths.
        """
        try:
            self._run_async(self._storage.write(path, content, self._zone_id))
            content_hash = hashlib.sha256(content).hexdigest()
            return content_hash
        except Exception as exc:
            logger.error("IPC write failed at %s: %s", path, exc)
            raise

    def read_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> bytes:
        """Read content by path (virtual filesystem mode).

        In VFS mode, ``content_hash`` is actually the virtual path.
        """
        path = content_hash  # In virtual FS mode, hash IS the path
        try:
            data: bytes = self._run_async(self._storage.read(path, self._zone_id))
            return data
        except FileNotFoundError as exc:
            raise NexusFileNotFoundError(path) from exc

    def delete_content(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> None:
        """Delete content — IPC never hard-deletes (moves to dead_letter)."""
        return

    def content_exists(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if a path exists (virtual FS mode)."""
        path = content_hash
        exists: bool = self._run_async(self._storage.exists(path, self._zone_id))
        return exists

    def get_content_size(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> int:
        """Get file size by reading the content (no separate size API)."""
        path = content_hash
        try:
            data = self._run_async(self._storage.read(path, self._zone_id))
            return len(data)
        except FileNotFoundError as exc:
            raise NexusFileNotFoundError(path) from exc

    def get_ref_count(
        self,
        content_hash: str,
        context: OperationContext | None = None,
    ) -> int:
        """Reference count — always 1 for path-based storage."""
        return 1

    # === Directory Operations ===

    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Create a directory in IPC storage."""
        self._run_async(self._storage.mkdir(path, self._zone_id))
        return

    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: OperationContext | None = None,
    ) -> None:
        """Remove directory — not supported for IPC (audit preservation)."""
        raise BackendError(
            "IPC directories cannot be removed (audit trail preservation)",
            backend="ipc",
        )

    def is_directory(
        self,
        path: str,
        context: OperationContext | None = None,
    ) -> bool:
        """Check if a path is a directory in IPC storage."""
        try:
            # If list_dir succeeds, the path is a directory
            self._run_async(self._storage.list_dir(path, self._zone_id))
            return True
        except FileNotFoundError:
            # Not a directory — could be a file or nonexistent
            return False
        except Exception:
            return False

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

    def _run_async(self, coro: Any) -> Any:
        """Run an async coroutine from sync Backend methods.

        The Backend ABC uses sync methods, but IPCStorageDriver is async.
        Dispatches to the background event loop created at construction
        time (immutable after init).
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._bg_loop)
        return future.result(timeout=self._timeout)
