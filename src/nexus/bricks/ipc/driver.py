"""VFS driver for the /agents/ mount point.

IPCVFSDriver satisfies ``ConnectorProtocol`` structurally (duck typing)
and is mounted at ``/agents`` via the PathRouter. It intercepts all file
operations on agent IPC paths and delegates to an ``IPCStorageDriver``.

This is an **external-content driver** (``EXTERNAL_CONTENT`` capability):
- ``read_content`` / ``write_content`` operate on paths, not content hashes
- ``list_dir`` returns agent names, subdirectories, or message files
- ``mkdir`` / ``rmdir`` / ``is_directory`` manage the IPC directory tree

The driver itself is intentionally thin — delivery logic (backpressure,
dedup, TTL, EventBus) lives in ``MessageSender`` and ``MessageProcessor``.

Issue: #1243, #370
Architecture: KERNEL-ARCHITECTURE.md
"""

import asyncio
import hashlib
import logging
import threading
from typing import TYPE_CHECKING, Any

from nexus.backends.backend import HandlerStatusResponse
from nexus.lib.response import HandlerResponse, timed_response

if TYPE_CHECKING:
    from nexus.bricks.ipc.storage.protocol import IPCStorageDriver
    from nexus.contracts.types import OperationContext

logger = logging.getLogger(__name__)


class IPCVFSDriver:
    """VFS driver for the ``/agents/`` mount point.

    Satisfies ``ConnectorProtocol`` structurally (duck-typed) so it can
    be mounted via ``PathRouter.add_mount()`` without inheriting from the
    ``Backend`` ABC in the backends layer — avoiding a cross-tier import.

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
        storage: "IPCStorageDriver",
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
        # ConnectorProtocol is sync, but IPCStorageDriver is async.
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

    @property
    def user_scoped(self) -> bool:
        return False

    @property
    def is_connected(self) -> bool:
        return True

    @property
    def is_passthrough(self) -> bool:
        return False

    @property
    def has_root_path(self) -> bool:
        return False

    @property
    def has_token_manager(self) -> bool:
        return False

    # === Connection (no-op for IPC) ===

    def connect(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        return HandlerStatusResponse(success=True, details={"backend": "ipc"})

    def disconnect(self, context: "OperationContext | None" = None) -> None:
        self.close()

    def check_connection(self, context: "OperationContext | None" = None) -> HandlerStatusResponse:
        return HandlerStatusResponse(success=True, details={"backend": "ipc"})

    # === Content Operations (path-oriented virtual FS) ===

    @timed_response
    def write_content(
        self,
        content: bytes,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[str]:
        """Write content and return a hash.

        For IPC, we store content at a generated path and return a
        content hash. Callers that need path-specific writes should
        use ``write_path()`` instead.
        """
        content_hash = hashlib.sha256(content).hexdigest()
        self._run_async(self._storage.write(f"/_cas/{content_hash}", content, self._zone_id))
        return HandlerResponse.ok(data=content_hash, backend_name="ipc")

    @timed_response
    def write_path(
        self,
        path: str,
        content: bytes,
        context: "OperationContext | None" = None,
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
            raise

    @timed_response
    def read_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
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

    @timed_response
    def delete_content(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Delete content — IPC never hard-deletes (moves to dead_letter)."""
        return HandlerResponse.ok(data=None, backend_name="ipc")

    @timed_response
    def content_exists(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[bool]:
        """Check if a path exists (virtual FS mode)."""
        path = content_hash
        exists = self._run_async(self._storage.exists(path, self._zone_id))
        return HandlerResponse.ok(data=exists, backend_name="ipc", path=path)

    @timed_response
    def get_content_size(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
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

    @timed_response
    def get_ref_count(
        self,
        content_hash: str,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[int]:
        """Reference count — always 1 for path-based storage."""
        return HandlerResponse.ok(data=1, backend_name="ipc")

    # === Directory Operations ===

    @timed_response
    def mkdir(
        self,
        path: str,
        parents: bool = False,
        exist_ok: bool = False,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Create a directory in IPC storage."""
        self._run_async(self._storage.mkdir(path, self._zone_id))
        return HandlerResponse.ok(data=None, backend_name="ipc", path=path)

    @timed_response
    def rmdir(
        self,
        path: str,
        recursive: bool = False,
        context: "OperationContext | None" = None,
    ) -> HandlerResponse[None]:
        """Remove directory — not supported for IPC (audit preservation)."""
        return HandlerResponse.error(
            message="IPC directories cannot be removed (audit trail preservation)",
            code=403,
            is_expected=True,
            backend_name="ipc",
            path=path,
        )

    @timed_response
    def is_directory(
        self,
        path: str,
        context: "OperationContext | None" = None,
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
        context: "OperationContext | None" = None,
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
        """Run an async coroutine from sync ConnectorProtocol methods.

        ConnectorProtocol is sync, but IPCStorageDriver is async.
        Dispatches to the background event loop created at construction
        time (immutable after init).
        """
        future = asyncio.run_coroutine_threadsafe(coro, self._bg_loop)
        return future.result(timeout=self._timeout)
