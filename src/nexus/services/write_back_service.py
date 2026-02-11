"""Write-Back Service for bidirectional sync (Issue #1129).

Subscribes to file events on mounted paths and writes changes back
to source backends. Handles conflict detection/resolution, retry,
and rate-limiting per backend.

Architecture:
- Event-driven: subscribes to EventBus for file write/delete/rename events
- Polling fallback: periodic sweep of pending backlog entries
- Rate-limited: per-backend asyncio.Semaphore
- Conflict-aware: LWW or fork policy via conflict_resolution module
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING, Any, Literal

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.services.conflict_resolution import detect_conflict, resolve_conflict

if TYPE_CHECKING:
    from nexus.core.event_bus import EventBusBase
    from nexus.services.change_log_store import ChangeLogStore
    from nexus.services.gateway import NexusFSGateway
    from nexus.services.sync_backlog_store import SyncBacklogEntry, SyncBacklogStore

logger = logging.getLogger(__name__)

# Event types that trigger write-back
_WRITE_BACK_EVENT_TYPES = frozenset(
    {
        FileEventType.FILE_WRITE,
        FileEventType.FILE_DELETE,
        FileEventType.FILE_RENAME,
        FileEventType.DIR_CREATE,
        FileEventType.DIR_DELETE,
    }
)


class WriteBackService:
    """Orchestrates bidirectional sync from Nexus to source backends.

    Responsibilities:
    1. Subscribe to event bus for write/delete/rename events on mounted paths
    2. Enqueue events to SyncBacklogStore
    3. Process pending entries: call backend write/delete/mkdir
    4. Handle conflicts via conflict_resolution module
    5. Rate-limit per backend via asyncio.Semaphore
    """

    def __init__(
        self,
        gateway: NexusFSGateway,
        event_bus: EventBusBase,
        backlog_store: SyncBacklogStore,
        change_log_store: ChangeLogStore,
        conflict_policy: Literal["lww", "fork"] = "lww",
        max_concurrent_per_backend: int = 10,
        poll_interval_seconds: float = 30.0,
    ) -> None:
        """Initialize WriteBackService.

        Args:
            gateway: NexusFSGateway for mount/file resolution
            event_bus: Event bus for subscribing to file events
            backlog_store: SyncBacklogStore for pending operations
            change_log_store: ChangeLogStore for conflict detection
            conflict_policy: "lww" (last writer wins) or "fork"
            max_concurrent_per_backend: Max concurrent write-backs per backend
            poll_interval_seconds: Interval between polling sweeps
        """
        self._gw = gateway
        self._event_bus = event_bus
        self._backlog_store = backlog_store
        self._change_log_store = change_log_store
        self._conflict_policy = conflict_policy
        self._max_concurrent = max_concurrent_per_backend
        self._poll_interval = poll_interval_seconds

        # Per-backend semaphores for rate limiting
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._running = False
        self._poll_task: asyncio.Task[None] | None = None
        self._subscribe_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start event subscription and polling loop."""
        if self._running:
            return
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._subscribe_task = asyncio.create_task(self._subscribe_loop())
        logger.info("[WRITE_BACK] Service started")

    async def stop(self) -> None:
        """Gracefully shut down the service."""
        self._running = False
        for task in (self._poll_task, self._subscribe_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._poll_task = None
        self._subscribe_task = None
        logger.info("[WRITE_BACK] Service stopped")

    async def _subscribe_loop(self) -> None:
        """Subscribe to event bus and enqueue matching events."""
        try:
            # Subscribe to all zones (use "*" or a default zone)
            async for event in self._event_bus.subscribe("*"):
                if not self._running:
                    break
                await self._on_file_event(event)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"[WRITE_BACK] Subscribe loop error: {e}")

    async def _on_file_event(self, event: FileEvent) -> None:
        """Handle an incoming file event: filter and enqueue if applicable."""
        event_type = FileEventType(event.type) if isinstance(event.type, str) else event.type
        if event_type not in _WRITE_BACK_EVENT_TYPES:
            return

        # Skip events that originated from write-back itself (avoid loops)
        if event_type in (
            FileEventType.SYNC_TO_BACKEND_COMPLETED,
            FileEventType.SYNC_TO_BACKEND_FAILED,
            FileEventType.SYNC_TO_BACKEND_REQUESTED,
        ):
            return

        mount_info = self._gw.get_mount_for_path(event.path)
        if mount_info is None:
            return

        # Skip readonly mounts
        if mount_info["readonly"]:
            return

        # Map event type to operation type
        op_type = self._event_to_operation(event_type)
        if op_type is None:
            return

        self._backlog_store.enqueue(
            path=event.path,
            backend_name=mount_info["backend_name"],
            zone_id=event.zone_id or "default",
            operation_type=op_type,
            content_hash=event.etag,
            new_path=event.old_path if event_type == FileEventType.FILE_RENAME else None,
        )

    async def _poll_loop(self) -> None:
        """Periodically process pending backlog entries."""
        while self._running:
            try:
                await self._process_all_backends()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[WRITE_BACK] Poll loop error: {e}")
            await asyncio.sleep(self._poll_interval)

    async def _process_all_backends(self) -> None:
        """Process pending entries for all writable backends."""
        mounts = self._gw.list_mounts()
        seen: set[tuple[str, str]] = set()
        for mount in mounts:
            if mount["readonly"]:
                continue
            backend_name = getattr(mount["backend"], "name", mount["backend_type"])
            # Process each backend+zone only once
            key = (backend_name, "default")
            if key in seen:
                continue
            seen.add(key)
            await self._process_pending(backend_name, "default")

    async def _process_pending(self, backend_name: str, zone_id: str) -> None:
        """Fetch and process a batch of pending entries for one backend."""
        entries = self._backlog_store.fetch_pending(backend_name, zone_id)
        if not entries:
            return

        sem = self._get_semaphore(backend_name)
        tasks = [self._process_entry(entry, sem) for entry in entries]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_entry(self, entry: SyncBacklogEntry, sem: asyncio.Semaphore) -> None:
        """Process a single backlog entry with semaphore rate limiting."""
        async with sem:
            if not self._backlog_store.mark_in_progress(entry.id):
                return  # Another worker claimed it
            try:
                await self._write_back_single(entry)
                self._backlog_store.mark_completed(entry.id)
                # Publish success event
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_COMPLETED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
            except Exception as e:
                logger.warning(f"[WRITE_BACK] Failed to write-back {entry.path}: {e}")
                self._backlog_store.mark_failed(entry.id, str(e))
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_FAILED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )

    async def _write_back_single(self, entry: SyncBacklogEntry) -> None:
        """Execute a single write-back operation to the backend.

        Steps:
        1. Resolve mount and backend for path
        2. Check for conflict (compare Nexus vs backend state)
        3. If conflict: resolve per policy
        4. Execute backend operation
        5. Update change log with new state

        Args:
            entry: SyncBacklogEntry to process

        Raises:
            RuntimeError: If mount not found or backend operation fails
        """
        mount_info = self._gw.get_mount_for_path(entry.path)
        if mount_info is None:
            raise RuntimeError(f"No mount found for path: {entry.path}")

        backend = mount_info["backend"]
        backend_path = mount_info["backend_path"]

        # Check for conflict before writing
        if entry.operation_type == "write":
            await self._handle_write(entry, backend, backend_path)
        elif entry.operation_type == "delete":
            await self._handle_delete(backend, backend_path)
        elif entry.operation_type == "mkdir":
            await self._handle_mkdir(backend, backend_path)
        else:
            raise RuntimeError(f"Unsupported operation: {entry.operation_type}")

    async def _handle_write(
        self,
        entry: SyncBacklogEntry,
        backend: Any,
        backend_path: str,
    ) -> None:
        """Handle write-back of a file to the backend."""
        # Step 1: Check for conflict
        last_synced = self._change_log_store.get_change_log(
            entry.path, entry.backend_name, entry.zone_id
        )

        # Get current backend file info for conflict detection
        backend_file_info = None
        if hasattr(backend, "get_file_info"):
            with contextlib.suppress(Exception):
                backend_file_info = backend.get_file_info(backend_path)

        if backend_file_info is not None and last_synced is not None:
            # Get Nexus file info for comparison
            nexus_meta = self._gw.metadata_get(entry.path)
            nexus_mtime = getattr(nexus_meta, "mtime", None) if nexus_meta else None
            nexus_hash = entry.content_hash

            is_conflict = detect_conflict(
                nexus_mtime=nexus_mtime,
                nexus_content_hash=nexus_hash,
                backend_file_info=backend_file_info,
                last_synced=last_synced,
            )

            if is_conflict:
                resolution = resolve_conflict(
                    nexus_mtime=nexus_mtime,
                    backend_mtime=backend_file_info.mtime,
                    policy=self._conflict_policy,
                )

                # Publish conflict event
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.CONFLICT_DETECTED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )

                if resolution == "backend_wins":
                    logger.info(
                        f"[WRITE_BACK] Conflict on {entry.path}: backend wins, skipping write-back"
                    )
                    return  # Skip write-back, backend version is newer

                if resolution == "fork":
                    logger.info(
                        f"[WRITE_BACK] Conflict on {entry.path}: fork — both versions preserved"
                    )
                    # For fork, we still write Nexus version but backend
                    # version remains. A future enhancement could create
                    # a .conflict copy.

        # Step 2: Read content from NexusFS and write to backend
        content = self._read_nexus_content(entry.path)
        if content is None:
            raise RuntimeError(f"Failed to read content for {entry.path}")

        result = backend.write_content(content)
        if hasattr(result, "success") and not result.success:
            raise RuntimeError(f"Backend write failed: {getattr(result, 'error', 'unknown')}")

        # Step 3: Update change log with new backend state
        new_hash = getattr(result, "data", None) if hasattr(result, "data") else None
        self._change_log_store.upsert_change_log(
            path=entry.path,
            backend_name=entry.backend_name,
            zone_id=entry.zone_id,
            content_hash=new_hash,
        )

    async def _handle_delete(self, backend: Any, backend_path: str) -> None:
        """Handle deletion of a file on the backend."""
        if hasattr(backend, "delete_content"):
            result = backend.delete_content(backend_path)
            if hasattr(result, "success") and not result.success:
                raise RuntimeError(f"Backend delete failed: {getattr(result, 'error', 'unknown')}")

    async def _handle_mkdir(self, backend: Any, backend_path: str) -> None:
        """Handle directory creation on the backend."""
        if hasattr(backend, "mkdir"):
            result = backend.mkdir(backend_path)
            if hasattr(result, "success") and not result.success:
                raise RuntimeError(f"Backend mkdir failed: {getattr(result, 'error', 'unknown')}")

    def _read_nexus_content(self, path: str) -> bytes | None:
        """Read file content from NexusFS.

        Args:
            path: Virtual file path

        Returns:
            File content bytes, or None if not found
        """
        try:
            meta = self._gw.metadata_get(path)
            if meta is None:
                return None
            content_hash = getattr(meta, "content_hash", None)
            if content_hash is None:
                return None
            result = self._gw.read(path)
            if isinstance(result, bytes):
                return result
            return getattr(result, "data", None) if result else None
        except Exception as e:
            logger.warning(f"[WRITE_BACK] Failed to read {path}: {e}")
            return None

    def _get_semaphore(self, backend_name: str) -> asyncio.Semaphore:
        """Get or create a rate-limiting semaphore for a backend."""
        if backend_name not in self._semaphores:
            self._semaphores[backend_name] = asyncio.Semaphore(self._max_concurrent)
        return self._semaphores[backend_name]

    def get_stats(self) -> dict[str, Any]:
        """Get write-back service statistics."""
        return {
            "running": self._running,
            "conflict_policy": self._conflict_policy,
            "max_concurrent_per_backend": self._max_concurrent,
            "poll_interval_seconds": self._poll_interval,
            "backlog_stats": self._backlog_store.get_stats(),
        }

    @staticmethod
    def _event_to_operation(event_type: FileEventType) -> str | None:
        """Map FileEventType to backlog operation type."""
        mapping = {
            FileEventType.FILE_WRITE: "write",
            FileEventType.FILE_DELETE: "delete",
            FileEventType.FILE_RENAME: "rename",
            FileEventType.DIR_CREATE: "mkdir",
            FileEventType.DIR_DELETE: "delete",
        }
        return mapping.get(event_type)
