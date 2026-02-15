"""Write-Back Service for bidirectional sync (Issue #1129, #1130).

Subscribes to file events on mounted paths and writes changes back
to source backends. Handles conflict detection/resolution, retry,
and rate-limiting per backend.

Architecture:
- Event-driven: subscribes to EventBus for file write/delete/rename events
- Polling fallback: periodic sweep of pending backlog entries
- Rate-limited: per-backend asyncio.Semaphore
- Conflict-aware: 6 configurable strategies via ConflictStrategy (Issue #1130)
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import posixpath
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from nexus.core.event_bus import FileEvent, FileEventType
from nexus.core.permissions import OperationContext
from nexus.services.conflict_resolution import (
    ConflictAbortError,
    ConflictContext,
    ConflictRecord,
    ConflictStatus,
    ConflictStrategy,
    ResolutionOutcome,
    detect_conflict,
    resolve_conflict,
)
from nexus.services.write_back_metrics import WriteBackMetrics

if TYPE_CHECKING:
    from nexus.core.event_bus import EventBusBase
    from nexus.services.change_log_store import ChangeLogStore
    from nexus.services.conflict_log_store import ConflictLogStore
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
        default_strategy: ConflictStrategy = ConflictStrategy.KEEP_NEWER,
        conflict_log_store: ConflictLogStore | None = None,
        max_concurrent_per_backend: int = 10,
        poll_interval_seconds: float = 30.0,
        batch_size: int = 50,
    ) -> None:
        """Initialize WriteBackService.

        Args:
            gateway: NexusFSGateway for mount/file resolution
            event_bus: Event bus for subscribing to file events
            backlog_store: SyncBacklogStore for pending operations
            change_log_store: ChangeLogStore for conflict detection
            default_strategy: Global default conflict strategy
            conflict_log_store: Optional store for conflict audit logging
            max_concurrent_per_backend: Max concurrent write-backs per backend
            poll_interval_seconds: Interval between polling sweeps
            batch_size: Max entries fetched per backend per poll cycle
        """
        self._gw = gateway
        self._event_bus = event_bus
        self._backlog_store = backlog_store
        self._change_log_store = change_log_store
        self._default_strategy = default_strategy
        self._conflict_log_store = conflict_log_store
        self._max_concurrent = max_concurrent_per_backend
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size

        # Per-backend semaphores for rate limiting
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._metrics = WriteBackMetrics()
        # Pre-built system context template — avoids UUID generation per-operation
        self._system_ctx = OperationContext(user="system", groups=[], is_system=True)
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
        """Process pending entries for all backends with pending work.

        Queries distinct (backend_name, zone_id) pairs from the backlog store
        rather than iterating mounts, ensuring non-default zones are processed.
        Backends are processed concurrently via asyncio.gather.
        """
        pairs = self._backlog_store.fetch_distinct_backend_zones()
        if not pairs:
            return
        await asyncio.gather(
            *(self._process_pending(name, zone) for name, zone in pairs),
            return_exceptions=True,
        )

    async def _process_pending(self, backend_name: str, zone_id: str) -> None:
        """Fetch and process a batch of pending entries for one backend."""
        entries = self._backlog_store.fetch_pending(backend_name, zone_id, limit=self._batch_size)
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
                self._metrics.record_push(entry.backend_name)
                # Publish success event
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_COMPLETED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
            except ConflictAbortError as e:
                logger.warning(f"[WRITE_BACK] Conflict ABORT on {entry.path}: {e}")
                self._backlog_store.mark_failed(entry.id, str(e))
                self._metrics.record_failure(entry.backend_name)
                await self._event_bus.publish(
                    FileEvent(
                        type=FileEventType.SYNC_TO_BACKEND_FAILED,
                        path=entry.path,
                        zone_id=entry.zone_id,
                    )
                )
            except Exception as e:
                logger.warning(f"[WRITE_BACK] Failed to write-back {entry.path}: {e}")
                self._backlog_store.mark_failed(entry.id, str(e))
                self._metrics.record_failure(entry.backend_name)
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
        3. If conflict: resolve per strategy
        4. Execute backend operation
        5. Update change log with new state

        Args:
            entry: SyncBacklogEntry to process

        Raises:
            RuntimeError: If mount not found or backend operation fails
            ConflictAbortError: If ABORT strategy is active
        """
        mount_info = self._gw.get_mount_for_path(entry.path)
        if mount_info is None:
            raise RuntimeError(f"No mount found for path: {entry.path}")

        backend = mount_info["backend"]
        backend_path = mount_info["backend_path"]

        # Check for conflict before writing
        if entry.operation_type == "write":
            await self._handle_write(entry, backend, backend_path, mount_info)
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
        mount_info: dict[str, Any],
    ) -> None:
        """Handle write-back of a file to the backend."""
        # Step 1: Check for conflict
        last_synced = self._change_log_store.get_change_log(
            entry.path, entry.backend_name, entry.zone_id
        )

        # Get current backend file info for conflict detection
        backend_file_info = None
        if hasattr(backend, "get_file_info"):
            try:
                result = await asyncio.to_thread(backend.get_file_info, backend_path)
                # Unwrap HandlerResponse if needed
                if hasattr(result, "data") and hasattr(result, "success"):
                    backend_file_info = result.data if result.success else None
                else:
                    backend_file_info = result
            except FileNotFoundError:
                pass  # File doesn't exist on backend yet — no conflict
            except Exception as exc:
                logger.debug("[WRITE_BACK] get_file_info failed for %s: %s", backend_path, exc)

        if backend_file_info is not None and last_synced is not None:
            # Get Nexus file info for comparison
            nexus_meta = self._gw.metadata_get(entry.path)
            nexus_mtime = getattr(nexus_meta, "mtime", None) if nexus_meta else None
            nexus_size = getattr(nexus_meta, "size", None) if nexus_meta else None
            nexus_hash = entry.content_hash

            is_conflict = detect_conflict(
                nexus_mtime=nexus_mtime,
                nexus_content_hash=nexus_hash,
                backend_file_info=backend_file_info,
                last_synced=last_synced,
            )

            if is_conflict:
                # Resolve per-mount strategy -> global default -> KEEP_NEWER
                strategy = self._resolve_strategy(mount_info)

                ctx = ConflictContext(
                    nexus_mtime=nexus_mtime,
                    nexus_size=nexus_size,
                    nexus_content_hash=nexus_hash,
                    backend_mtime=backend_file_info.mtime,
                    backend_size=getattr(backend_file_info, "size", None),
                    backend_content_hash=getattr(backend_file_info, "content_hash", None),
                    path=entry.path,
                    backend_name=entry.backend_name,
                    zone_id=entry.zone_id,
                )

                should_proceed = await self._resolve_and_act_on_conflict(entry, ctx, strategy)
                if not should_proceed:
                    return

        # Step 2: Read content from NexusFS and write to backend
        content = self._read_nexus_content(entry.path)
        if content is None:
            raise RuntimeError(f"Failed to read content for {entry.path}")

        op_ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        result = await asyncio.to_thread(backend.write_content, content, op_ctx)
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

    async def _resolve_and_act_on_conflict(
        self,
        entry: SyncBacklogEntry,
        ctx: ConflictContext,
        strategy: ConflictStrategy,
    ) -> bool:
        """Resolve conflict and return True if write should proceed.

        Args:
            entry: The backlog entry being processed
            ctx: ConflictContext with all metadata
            strategy: Which resolution strategy to apply

        Returns:
            True if the write-back should proceed, False to skip

        Raises:
            ConflictAbortError: If ABORT strategy is applied
        """
        outcome = resolve_conflict(ctx, strategy)

        # Record conflict metric (auto-resolved unless ABORT)
        auto_resolved = outcome != ResolutionOutcome.ABORT
        self._metrics.record_conflict(entry.backend_name, auto_resolved=auto_resolved)

        # Publish conflict event
        await self._event_bus.publish(
            FileEvent(
                type=FileEventType.CONFLICT_DETECTED,
                path=entry.path,
                zone_id=entry.zone_id,
            )
        )

        # Build and log conflict record
        conflict_copy_path: str | None = None
        if outcome == ResolutionOutcome.RENAME_CONFLICT:
            conflict_copy_path = self._generate_conflict_copy_path(entry.path, entry.backend_name)

        record = ConflictRecord(
            id=self._make_conflict_id(),
            path=ctx.path,
            backend_name=ctx.backend_name,
            zone_id=ctx.zone_id,
            strategy=strategy,
            outcome=outcome,
            nexus_content_hash=ctx.nexus_content_hash,
            nexus_mtime=ctx.nexus_mtime,
            nexus_size=ctx.nexus_size,
            backend_content_hash=ctx.backend_content_hash,
            backend_mtime=ctx.backend_mtime,
            backend_size=ctx.backend_size,
            conflict_copy_path=conflict_copy_path,
            status=ConflictStatus.AUTO_RESOLVED,
            resolved_at=datetime.now(UTC),
        )

        if self._conflict_log_store is not None:
            try:
                self._conflict_log_store.log_conflict(record)
            except Exception as e:
                logger.warning(f"[WRITE_BACK] Failed to log conflict: {e}")

        # Act on outcome
        match outcome:
            case ResolutionOutcome.ABORT:
                raise ConflictAbortError(
                    f"Conflict on {entry.path}: ABORT strategy — write-back halted"
                )
            case ResolutionOutcome.BACKEND_WINS:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: backend wins, skipping write-back"
                )
                return False
            case ResolutionOutcome.NEXUS_WINS:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: nexus wins, proceeding with write-back"
                )
                return True
            case ResolutionOutcome.RENAME_CONFLICT:
                logger.info(
                    f"[WRITE_BACK] Conflict on {entry.path}: creating conflict "
                    f"copy at {conflict_copy_path}"
                )
                self._create_conflict_copy(entry.path, conflict_copy_path)  # type: ignore[arg-type]
                return True
            case _:
                logger.warning(
                    "[WRITE_BACK] Unhandled resolution outcome %s on %s, skipping",
                    outcome,
                    entry.path,
                )
                return False

    def _resolve_strategy(self, mount_info: dict[str, Any]) -> ConflictStrategy:
        """Resolve the conflict strategy for a mount.

        Resolution chain: mount.conflict_strategy -> global default -> KEEP_NEWER
        """
        mount_strategy = mount_info.get("conflict_strategy")
        if mount_strategy is not None:
            try:
                return ConflictStrategy(mount_strategy)
            except ValueError:
                pass
        return self._default_strategy

    def _create_conflict_copy(self, original_path: str, conflict_path: str) -> None:
        """Create a NexusFS-side conflict copy of the file.

        Reads the current content and writes it to the conflict copy path.
        NexusFS-side only (CAS), near-free — following the Syncthing model.
        """
        try:
            content = self._read_nexus_content(original_path)
            if content is not None:
                self._gw.write(conflict_path, content)
        except Exception as e:
            logger.warning(f"[WRITE_BACK] Failed to create conflict copy: {e}")

    @staticmethod
    def _generate_conflict_copy_path(path: str, backend_name: str) -> str:
        """Generate a .sync-conflict copy path.

        Format: {dir}/{stem}.sync-conflict-{ISO timestamp}-{backend_name}{ext}
        """
        dir_part = posixpath.dirname(path)
        base = posixpath.basename(path)
        stem, ext = posixpath.splitext(base)
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        conflict_name = f"{stem}.sync-conflict-{timestamp}-{backend_name}{ext}"
        return posixpath.join(dir_part, conflict_name)

    @staticmethod
    def _make_conflict_id() -> str:
        """Generate a UUID for a conflict record."""
        return str(uuid.uuid4())

    async def _handle_delete(self, backend: Any, backend_path: str) -> None:
        """Handle deletion of a file on the backend.

        Tries path-based delete() first (connector backends like local_connector),
        falling back to delete_content() for CAS backends.
        """
        ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        if hasattr(backend, "delete"):
            result = await asyncio.to_thread(backend.delete, backend_path, ctx)
        elif hasattr(backend, "delete_content"):
            result = await asyncio.to_thread(backend.delete_content, backend_path, ctx)
        else:
            raise RuntimeError(
                f"Backend {type(backend).__name__} supports neither delete nor delete_content"
            )
        if hasattr(result, "success") and not result.success:
            raise RuntimeError(f"Backend delete failed: {getattr(result, 'error', 'unknown')}")

    async def _handle_mkdir(self, backend: Any, backend_path: str) -> None:
        """Handle directory creation on the backend."""
        if not hasattr(backend, "mkdir"):
            raise RuntimeError(f"Backend {type(backend).__name__} does not support mkdir")
        ctx = dataclasses.replace(self._system_ctx, backend_path=backend_path)
        result = await asyncio.to_thread(backend.mkdir, backend_path, context=ctx)
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
            "default_strategy": str(self._default_strategy),
            "max_concurrent_per_backend": self._max_concurrent,
            "poll_interval_seconds": self._poll_interval,
            "backlog_stats": self._backlog_store.get_stats(),
            "metrics": self._metrics.snapshot(),
        }

    def get_mount_for_path(self, path: str) -> dict[str, Any] | None:
        """Resolve mount info for a virtual path (public API for push endpoint)."""
        return self._gw.get_mount_for_path(path)

    def get_metrics_snapshot(self) -> dict[str, Any]:
        """Return a thread-safe copy of write-back metrics."""
        return self._metrics.snapshot()

    async def push_mount(self, backend_name: str, zone_id: str) -> None:
        """Trigger an immediate push for a specific backend/zone pair.

        Processes all pending backlog entries for the given backend and zone.
        """
        await self._process_pending(backend_name, zone_id)

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
